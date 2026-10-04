package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeProxy answers with its name and records the key and body it received.
type fakeProxy struct {
	name    string
	gotKey  string
	gotBody string
	block   chan struct{}
	started chan struct{}
}

func (f *fakeProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.gotKey = strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	body, _ := io.ReadAll(r.Body)
	f.gotBody = string(body)
	if r.URL.Path == "/v1/models" {
		_, _ = io.WriteString(w, `{"data":[{"id":"model"}]}`)
		return
	}
	if f.block != nil {
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(f.started)
		select {
		case <-f.block:
		case <-r.Context().Done():
		}
		return
	}
	_, _ = io.WriteString(w, f.name)
}

func newTestSwitcher(t *testing.T, m1URL, localURL string) *switcher {
	t.Helper()
	m1, err := newUpstream("m1", m1URL, "m1-key")
	if err != nil {
		t.Fatal(err)
	}
	local, err := newUpstream("local", localURL, "local-key")
	if err != nil {
		t.Fatal(err)
	}
	return &switcher{m1: m1, local: local, clientKey: "local-key", health: newHealth(2, 2), probe: http.DefaultClient}
}

func send(t *testing.T, s *switcher, key string) (int, string) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/messages", strings.NewReader(`{"prompt":"hi"}`))
	req.Header.Set("Authorization", "Bearer "+key)
	rec := httptest.NewRecorder()
	s.ServeHTTP(rec, req)
	return rec.Code, rec.Body.String()
}

func TestRoutesByM1HealthAndSwapsKeys(t *testing.T) {
	m1, local := &fakeProxy{name: "m1"}, &fakeProxy{name: "local"}
	m1Server, localServer := httptest.NewServer(m1), httptest.NewServer(local)
	defer m1Server.Close()
	defer localServer.Close()
	s := newTestSwitcher(t, m1Server.URL, localServer.URL)

	if _, body := send(t, s, "local-key"); body != "local" {
		t.Fatalf("before any healthy check got %q, want local", body)
	}
	s.health.record(s.probeM1(t.Context()))
	if _, body := send(t, s, "local-key"); body != "local" {
		t.Fatalf("after one healthy check got %q, want local", body)
	}
	s.health.record(s.probeM1(t.Context()))
	if _, body := send(t, s, "local-key"); body != "m1" {
		t.Fatalf("after two healthy checks got %q, want m1", body)
	}
	if m1.gotKey != "m1-key" || m1.gotBody != `{"prompt":"hi"}` {
		t.Fatalf("M1 received key %q body %q", m1.gotKey, m1.gotBody)
	}
	if local.gotKey != "local-key" {
		t.Fatalf("local received key %q", local.gotKey)
	}

	s.health.record(false)
	if _, body := send(t, s, "local-key"); body != "m1" {
		t.Fatalf("one failed check should not switch; got %q", body)
	}
	s.health.record(false)
	if _, body := send(t, s, "local-key"); body != "local" {
		t.Fatalf("after two failed checks got %q, want local", body)
	}
}

func TestFallsBackWhenM1RequestCannotConnect(t *testing.T) {
	local := &fakeProxy{name: "local"}
	localServer := httptest.NewServer(local)
	defer localServer.Close()
	dead := httptest.NewServer(http.NotFoundHandler())
	deadURL := dead.URL
	dead.Close()
	s := newTestSwitcher(t, deadURL, localServer.URL)
	s.health.record(true)
	s.health.record(true)

	code, body := send(t, s, "local-key")
	if code != http.StatusOK || body != "local" {
		t.Fatalf("got %d %q, want the local proxy", code, body)
	}
	if local.gotBody != `{"prompt":"hi"}` {
		t.Fatalf("retried request lost its body: %q", local.gotBody)
	}
	if up, _ := s.health.state(); up {
		t.Fatal("M1 should be marked down after a connection failure")
	}
}

func TestCancelsInFlightM1RequestWhenMarkedDown(t *testing.T) {
	m1 := &fakeProxy{name: "m1", block: make(chan struct{}), started: make(chan struct{})}
	m1Server := httptest.NewServer(m1)
	defer m1Server.Close()
	defer close(m1.block)
	s := newTestSwitcher(t, m1Server.URL, m1Server.URL)
	s.health.record(true)
	s.health.record(true)

	done := make(chan struct{})
	go func() {
		send(t, s, "local-key")
		close(done)
	}()
	<-m1.started
	s.health.fail()
	<-done
}

func TestRejectsWrongKey(t *testing.T) {
	s := newTestSwitcher(t, "http://127.0.0.1:1", "http://127.0.0.1:1")
	if code, _ := send(t, s, "wrong"); code != http.StatusUnauthorized {
		t.Fatalf("got %d, want 401", code)
	}
}
