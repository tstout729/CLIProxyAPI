// Command proxy-switch sends each client request to the M1 gateway while it is
// healthy and to this Mac's identical proxy otherwise, so long-running Claude
// Code and Codex sessions fail over and fail back without a restart.
//
// Clients authenticate with the local proxy key. The switch replaces it with the
// key that belongs to whichever proxy serves the request.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	log "github.com/sirupsen/logrus"
)

// maxReplayBody bounds how much of a request body is kept so a failed M1
// attempt can be repeated on the local proxy.
const maxReplayBody = 64 << 20

type upstream struct {
	name  string
	url   *url.URL
	key   string
	proxy *httputil.ReverseProxy
}

// health tracks whether the M1 should receive new requests. Requests already in
// flight to the M1 are cancelled when it is marked down so clients retry and
// land on the local proxy instead of hanging on a dead tunnel.
type health struct {
	mu        sync.Mutex
	up        bool
	successes int
	failures  int
	down      chan struct{}
	upAfter   int
	downAfter int
}

func newHealth(upAfter, downAfter int) *health {
	return &health{down: make(chan struct{}), upAfter: upAfter, downAfter: downAfter}
}

// state reports whether the M1 is up and a channel closed when it next goes down.
func (h *health) state() (bool, <-chan struct{}) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.up, h.down
}

// record applies one probe result and returns true when the state changed.
func (h *health) record(ok bool) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if ok {
		h.failures = 0
		h.successes++
		if !h.up && h.successes >= h.upAfter {
			h.up = true
			h.down = make(chan struct{})
			return true
		}
		return false
	}
	h.successes = 0
	h.failures++
	if h.up && h.failures >= h.downAfter {
		h.markDownLocked()
		return true
	}
	return false
}

// fail marks the M1 down immediately after a request could not reach it.
func (h *health) fail() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.successes = 0
	if !h.up {
		return false
	}
	h.markDownLocked()
	return true
}

func (h *health) markDownLocked() {
	h.up = false
	close(h.down)
}

type switcher struct {
	m1, local *upstream
	clientKey string
	health    *health
	probe     *http.Client
}

func newUpstream(name, rawURL, key string) (*upstream, error) {
	target, err := url.Parse(strings.TrimRight(rawURL, "/"))
	if err != nil {
		return nil, err
	}
	u := &upstream{name: name, url: target, key: key}
	u.proxy = &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(target)
			r.Out.Host = target.Host
			if r.Out.Header.Get("Authorization") != "" {
				r.Out.Header.Set("Authorization", "Bearer "+key)
			}
			if r.Out.Header.Get("X-Api-Key") != "" {
				r.Out.Header.Set("X-Api-Key", key)
			}
		},
		// Stream server-sent events to the client as they arrive.
		FlushInterval: -1,
	}
	return u, nil
}

func (s *switcher) authorized(r *http.Request) bool {
	presented := r.Header.Get("X-Api-Key")
	if bearer, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer "); ok {
		presented = bearer
	}
	return presented != "" && subtle.ConstantTimeCompare([]byte(presented), []byte(s.clientKey)) == 1
}

func (s *switcher) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/switch/status" {
		up, _ := s.health.state()
		w.Header().Set("Content-Type", "application/json")
		if up {
			_, _ = io.WriteString(w, `{"route":"m1"}`+"\n")
		} else {
			_, _ = io.WriteString(w, `{"route":"local"}`+"\n")
		}
		return
	}
	if !s.authorized(r) {
		http.Error(w, "invalid proxy key", http.StatusUnauthorized)
		return
	}
	up, down := s.health.state()
	if !up {
		s.local.proxy.ServeHTTP(w, r)
		return
	}

	// Keep the body so the request can be repeated locally if the M1 is unreachable.
	var body []byte
	if r.Body != nil && r.Body != http.NoBody {
		var err error
		body, err = io.ReadAll(io.LimitReader(r.Body, maxReplayBody+1))
		if errClose := r.Body.Close(); errClose != nil {
			log.Debugf("closing request body: %v", errClose)
		}
		if err != nil {
			http.Error(w, "could not read request", http.StatusBadRequest)
			return
		}
		if len(body) > maxReplayBody {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
			return
		}
	}
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	go func() {
		select {
		case <-down:
			cancel()
		case <-ctx.Done():
		}
	}()

	attempt := r.Clone(ctx)
	attempt.Body = io.NopCloser(bytes.NewReader(body))
	attempt.ContentLength = int64(len(body))
	failed := false
	m1 := *s.m1.proxy
	m1.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		// No response was written yet, so the local proxy can still answer.
		failed = true
		if s.health.fail() {
			log.Warnf("M1 request failed (%v); routing to the local proxy", err)
		}
	}
	m1.ServeHTTP(w, attempt)
	if !failed {
		return
	}
	if r.Context().Err() != nil {
		return
	}
	retry := r.Clone(r.Context())
	retry.Body = io.NopCloser(bytes.NewReader(body))
	retry.ContentLength = int64(len(body))
	s.local.proxy.ServeHTTP(w, retry)
}

// probeM1 succeeds when the M1 accepts its key and lists at least one model.
func (s *switcher) probeM1(ctx context.Context) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.m1.url.String()+"/v1/models", nil)
	if err != nil {
		return false
	}
	req.Header.Set("Authorization", "Bearer "+s.m1.key)
	resp, err := s.probe.Do(req)
	if err != nil {
		return false
	}
	defer func() {
		if errClose := resp.Body.Close(); errClose != nil {
			log.Debugf("closing probe response: %v", errClose)
		}
	}()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return err == nil && resp.StatusCode == http.StatusOK && bytes.Contains(payload, []byte(`"id"`))
}

func (s *switcher) watch(ctx context.Context, interval time.Duration) {
	check := func() {
		if s.health.record(s.probeM1(ctx)) {
			if up, _ := s.health.state(); up {
				log.Infof("M1 proxy is healthy; new requests go to %s", s.m1.url)
			} else {
				log.Warnf("M1 proxy is unreachable; new requests go to %s", s.local.url)
			}
		}
	}
	check()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			check()
		}
	}
}

func readKey(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if info.Mode().Perm()&0o077 != 0 {
		return "", errors.New(path + " must be private (mode 0600)")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	key := strings.TrimSpace(string(raw))
	if key == "" {
		return "", errors.New(path + " is empty")
	}
	return key, nil
}

func main() {
	if err := run(); err != nil {
		log.Error(err)
		os.Exit(1)
	}
}

func run() error {
	configDir := filepath.Join(os.Getenv("HOME"), ".config/cliproxyapi-custom")
	listen := flag.String("listen", "127.0.0.1:18320", "address for clients")
	m1URL := flag.String("m1", "http://127.0.0.1:18318", "M1 gateway through the SSH tunnel")
	localURL := flag.String("local", "http://127.0.0.1:8318", "proxy on this Mac")
	m1KeyFile := flag.String("m1-key-file", filepath.Join(configDir, "m1-client-api-key"), "M1 inference key")
	localKeyFile := flag.String("local-key-file", filepath.Join(configDir, "client-api-key"), "local inference key; clients use it too")
	interval := flag.Duration("interval", 5*time.Second, "M1 health check interval")
	flag.Parse()

	m1Key, err := readKey(*m1KeyFile)
	if err != nil {
		return fmt.Errorf("read M1 key: %w", err)
	}
	localKey, err := readKey(*localKeyFile)
	if err != nil {
		return fmt.Errorf("read local key: %w", err)
	}
	m1, err := newUpstream("m1", *m1URL, m1Key)
	if err != nil {
		return fmt.Errorf("parse M1 URL: %w", err)
	}
	local, err := newUpstream("local", *localURL, localKey)
	if err != nil {
		return fmt.Errorf("parse local URL: %w", err)
	}
	s := &switcher{
		m1: m1, local: local, clientKey: localKey,
		// Two failed checks mark the M1 down; two good ones bring it back.
		health: newHealth(2, 2),
		probe:  &http.Client{Timeout: 3 * time.Second},
	}
	go s.watch(context.Background(), *interval)
	log.Infof("proxy switch listening on %s", *listen)
	server := &http.Server{Addr: *listen, Handler: s, ReadHeaderTimeout: 30 * time.Second}
	return server.ListenAndServe()
}
