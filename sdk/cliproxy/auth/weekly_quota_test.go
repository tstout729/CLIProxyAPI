package auth

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func weeklyTestCache(now time.Time) *WeeklyQuotaCache {
	c := newWeeklyQuotaCache()
	c.nowFunc = func() time.Time { return now }
	return c
}

func weeklyTestObserve(c *WeeklyQuotaCache, auth *Auth, now, reset time.Time, remaining float64) {
	c.observations[auth.ID] = weeklyQuotaObservation{Epoch: auth.RegistrationEpoch, ObservedAt: now, LastAttempt: now, Buckets: []WeeklyQuotaBucket{{Scope: "all", Window: "weekly", ResetAt: &reset, RemainingPercent: remaining}}}
}

func TestWeeklyResetFirstSelectionAndFallback(t *testing.T) {
	now := time.Date(2026, 10, 2, 22, 0, 0, 0, time.UTC)
	early, later, unknown := &Auth{ID: "z-early"}, &Auth{ID: "a-later"}, &Auth{ID: "b-unknown"}
	cache := weeklyTestCache(now)
	selector := &WeeklyResetFirstSelector{Cache: cache}
	weeklyTestObserve(cache, early, now, now.Add(time.Hour), 25)
	weeklyTestObserve(cache, later, now, now.Add(24*time.Hour), 90)
	pick := func(auths []*Auth, want string) {
		t.Helper()
		got, err := selector.Pick(context.Background(), "claude", "claude-sonnet-4.5", cliproxyexecutor.Options{}, auths)
		if err != nil || got.ID != want {
			t.Fatalf("Pick = %v, %v; want %s", got, err, want)
		}
	}
	pick([]*Auth{unknown, later, early}, early.ID)
	// Retry exclusions supplied by Manager retain deterministic fallback.
	pick([]*Auth{unknown, later}, later.ID)
	weeklyTestObserve(cache, early, now, now.Add(time.Hour), 0)
	pick([]*Auth{early, later, unknown}, later.ID)
	later.Disabled = true
	pick([]*Auth{early, later, unknown}, unknown.ID)
	unknown.Unavailable = true
	if _, err := selector.Pick(context.Background(), "claude", "", cliproxyexecutor.Options{}, []*Auth{early, later, unknown}); err == nil {
		t.Fatal("expected unavailable error")
	}
}

func TestWeeklyResetFirstModelScopeResetAndStale(t *testing.T) {
	now := time.Date(2026, 10, 2, 22, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	auth := &Auth{ID: "a"}
	reset := now.Add(time.Hour)
	weeklyTestObserve(cache, auth, now, now.Add(24*time.Hour), 50)
	o := cache.observations[auth.ID]
	o.Buckets = append(o.Buckets, WeeklyQuotaBucket{Scope: "opus", Window: "weekly", ResetAt: &reset, RemainingPercent: 0})
	cache.observations[auth.ID] = o
	if _, blocked := cache.rank(auth, "claude-sonnet-4.5(high)", now); blocked {
		t.Fatal("Opus limit blocked Sonnet")
	}
	if _, blocked := cache.rank(auth, "claude-opus-4.5", now); !blocked {
		t.Fatal("Opus should be exhausted")
	}
	if _, blocked := cache.rank(auth, "claude-opus-4.5", reset); blocked {
		t.Fatal("passed reset must become unknown")
	}
	if _, blocked := cache.rank(auth, "claude-opus-4.5", now.Add(weeklyQuotaStaleTTL)); blocked {
		t.Fatal("stale observation must be eligible unknown fallback")
	}
	if ranked, _ := cache.rank(auth, "claude-sonnet-4.5", now.Add(weeklyQuotaStaleTTL)); !ranked.IsZero() {
		t.Fatal("stale reset must not outrank current observations")
	}
	// Five-hour capacity excludes the account even with remaining weekly balance.
	o.Buckets = append(o.Buckets, WeeklyQuotaBucket{Scope: "all", Window: "five-hour", ResetAt: &reset, RemainingPercent: 0})
	cache.observations[auth.ID] = o
	if _, blocked := cache.rank(auth, "claude-sonnet-4.5", now); !blocked {
		t.Fatal("five-hour exhaustion was ignored")
	}
	if got := cache.observations[auth.ID].Buckets[1].RemainingPercent; got != 0 {
		t.Fatal("reset fabricated a replenished balance")
	}
}

func TestWeeklyFableFamilyExhaustionDoesNotBlockSonnet(t *testing.T) {
	now := time.Now()
	cache := weeklyTestCache(now)
	a := &Auth{ID: "fable-family-test"}
	buckets, err := parseWeeklyQuota("claude", []byte(`{"limits":[{"kind":"weekly_scoped","percent":100,"is_active":true,"resets_at":"2099-10-03T22:00:00Z","scope":{"model":{"display_name":"Fable 5"}}}]}`), now)
	if err != nil {
		t.Fatal(err)
	}
	cache.observations[a.ID] = weeklyQuotaObservation{ObservedAt: now, Buckets: buckets}
	if _, blocked := cache.rank(a, "claude-fable-5", now); !blocked {
		t.Fatal("Fable 5 family exhaustion was ignored")
	}
	if _, blocked := cache.rank(a, "claude-sonnet-4.5", now); blocked {
		t.Fatal("Fable exhaustion blocked Sonnet")
	}
}

func TestWeeklyWebsocketPreferenceKeepsDefaultOAuthAffinity(t *testing.T) {
	now := time.Now()
	cache := weeklyTestCache(now)
	a := &Auth{ID: "z-default-ws", Provider: "codex", Metadata: map[string]any{"access_token": "test"}}
	b := &Auth{ID: "a-explicit-ws", Provider: "codex", Metadata: map[string]any{"access_token": "test", "websockets": true}}
	ctx := cliproxyexecutor.WithDownstreamWebsocket(context.Background())
	if len(preferCodexWebsocketAuths(ctx, "codex", []*Auth{a, b})) != 2 {
		t.Fatal("default OAuth capability was excluded")
	}
	weeklyTestObserve(cache, a, now, now.Add(time.Hour), 25)
	weeklyTestObserve(cache, b, now, now.Add(24*time.Hour), 25)
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{Cache: cache})
	defer affinity.Stop()
	opts := cliproxyexecutor.Options{Headers: http.Header{"Session_id": []string{"default-websocket-thread"}}}
	for i := 0; i < 2; i++ {
		got, err := affinity.Pick(ctx, "codex", "gpt-5-codex", opts, []*Auth{a, b})
		if err != nil || got.ID != a.ID {
			t.Fatalf("default WS affinity = %v, %v", got, err)
		}
		weeklyTestObserve(cache, b, now, now.Add(30*time.Minute), 25)
	}
	a.Metadata["websockets"] = false
	if authWebsocketsEnabled(a) {
		t.Fatal("explicit credential opt-out was ignored")
	}
}

func TestWeeklyManagerExhaustionBeforePriorityAndScheduler(t *testing.T) {
	now := time.Now()
	manager := NewManager(nil, &WeeklyResetFirstSelector{}, nil)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	manager.RegisterExecutor(&replaceAwareExecutor{id: "claude"})
	if manager.useSchedulerFastPath() {
		t.Fatal("weekly selector bypassed by builtin scheduler")
	}
	high, _ := manager.Register(context.Background(), &Auth{ID: "high", Provider: "claude", Attributes: map[string]string{"priority": "9"}})
	low, _ := manager.Register(context.Background(), &Auth{ID: "low", Provider: "claude"})
	weeklyTestObserve(manager.weeklyQuota, high, now, now.Add(time.Hour), 0)
	weeklyTestObserve(manager.weeklyQuota, low, now, now.Add(24*time.Hour), 25)
	got, err := manager.SelectAuth(context.Background(), "claude", "", cliproxyexecutor.Options{})
	if err != nil || got.ID != low.ID {
		t.Fatalf("priority exhausted fallback = %v, %v", got, err)
	}
	weeklyTestObserve(manager.weeklyQuota, high, now, now.Add(7*24*time.Hour), 25)
	got, err = manager.SelectAuth(context.Background(), "claude", "", cliproxyexecutor.Options{})
	if err != nil || got.ID != high.ID {
		t.Fatalf("explicit priority override = %v, %v", got, err)
	}
}

func TestWeeklyAffinityKeepsThreadAcrossModelsAndRebindsOnExhaustion(t *testing.T) {
	now := time.Now()
	cache := weeklyTestCache(now)
	cache.nowFunc = func() time.Time { return now }
	weekly := &WeeklyResetFirstSelector{Cache: cache}
	affinity := NewSessionAffinitySelector(weekly)
	defer affinity.Stop()
	a, b := &Auth{ID: "a"}, &Auth{ID: "b"}
	weeklyTestObserve(cache, a, now, now.Add(time.Hour), 20)
	weeklyTestObserve(cache, b, now, now.Add(24*time.Hour), 90)
	opts := cliproxyexecutor.Options{Headers: http.Header{"X-Session-Id": []string{"thread-one"}}}
	pick := func(model, want string) {
		t.Helper()
		got, err := affinity.Pick(context.Background(), "claude", model, opts, []*Auth{a, b})
		if err != nil || got.ID != want {
			t.Fatalf("thread pick = %v, %v, want %s", got, err, want)
		}
	}
	pick("claude-sonnet-4.5", a.ID)
	// New reset ordering does not rotate a healthy bound thread.
	weeklyTestObserve(cache, b, now, now.Add(30*time.Minute), 90)
	pick("claude-opus-4.5", a.ID)
	if got, status := affinity.LookupAffinity("claude", "claude-opus-4.5", "thread-one"); got != a.ID || status != "bound" {
		t.Fatalf("Lookup = %s/%s", got, status)
	}
	// A passed reset and stale balance also leave a healthy established thread
	// bound; only upstream capacity or request availability decides failover.
	now = now.Add(2 * time.Hour)
	weeklyTestObserve(cache, b, now, now.Add(30*time.Minute), 90)
	pick("claude-opus-4.5", a.ID)
	weeklyTestObserve(cache, a, now, now.Add(time.Hour), 0)
	pick("claude-opus-4.5", b.ID)
	weeklyTestObserve(cache, a, now, now.Add(10*time.Minute), 90)
	pick("claude-sonnet-4.5", b.ID)
	// The same thread ID on another provider cannot use the Claude binding.
	got, err := affinity.Pick(context.Background(), "codex", "gpt-5.4", opts, []*Auth{a, b})
	if err != nil || got.ID != a.ID {
		t.Fatalf("provider isolation = %v, %v", got, err)
	}
}

func TestWeeklyManagerThreadModelSupportAndRequestErrorFallback(t *testing.T) {
	now := time.Now()
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{})
	defer affinity.Stop()
	manager := NewManager(nil, affinity, nil)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	manager.RegisterExecutor(&replaceAwareExecutor{id: "claude"})
	a, _ := manager.Register(context.Background(), &Auth{ID: "weekly-model-test-a", Provider: "claude", Status: StatusActive})
	b, _ := manager.Register(context.Background(), &Auth{ID: "weekly-model-test-b", Provider: "claude", Status: StatusActive})
	sonnet, opus := "claude-sonnet-weekly-model-test", "claude-opus-weekly-model-test"
	registry.GetGlobalRegistry().RegisterClient(a.ID, "claude", []*registry.ModelInfo{{ID: sonnet}})
	registry.GetGlobalRegistry().RegisterClient(b.ID, "claude", []*registry.ModelInfo{{ID: sonnet}, {ID: opus}})
	t.Cleanup(func() {
		registry.GetGlobalRegistry().UnregisterClient(a.ID)
		registry.GetGlobalRegistry().UnregisterClient(b.ID)
	})
	weeklyTestObserve(manager.weeklyQuota, a, now, now.Add(time.Hour), 25)
	weeklyTestObserve(manager.weeklyQuota, b, now, now.Add(24*time.Hour), 90)
	opts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.DerivedSessionIDMetadataKey: "weekly-model-thread"}}
	pick := func(model, want string) {
		t.Helper()
		got, err := manager.SelectAuth(context.Background(), "claude", model, opts)
		if err != nil || got.ID != want {
			t.Fatalf("model support pick = %v, %v, want %s", got, err, want)
		}
	}
	pick(sonnet, a.ID)
	pick(opus, b.ID)
	pick(sonnet, b.ID)
	manager.MarkResult(context.Background(), Result{AuthID: b.ID, Provider: "claude", Model: sonnet, CredentialScope: true, Success: false, Error: &Error{HTTPStatus: http.StatusTooManyRequests, Message: "capacity exhausted"}, Options: opts})
	pick(sonnet, a.ID)
}

func TestParseWeeklyQuotaUsesActualWindowsAndModelScope(t *testing.T) {
	now := time.Unix(1800000000, 0)
	claude := []byte(`{"seven_day":{"utilization":80,"resets_at":"2026-10-03T22:00:00Z"},"five_hour":{"utilization":100,"resets_at":"2026-10-03T01:00:00Z"},"seven_day_sonnet":{"utilization":100,"resets_at":"2026-10-04T22:00:00Z"},"extra_usage":{"is_enabled":true,"monthly_limit":10},"limits":[{"kind":"weekly_scoped","percent":98,"is_active":true,"resets_at":"2026-10-03T22:00:00Z","scope":{"model":{"id":"claude-fable-5","display_name":"Fable 5"}}},{"kind":"weekly_scoped","percent":100,"is_active":false,"scope":{"model":{"display_name":"Fable"}}}]}`)
	buckets, err := parseWeeklyQuota("claude", claude, now)
	if err != nil || len(buckets) != 4 || buckets[0].RemainingPercent != 20 || buckets[1].Scope != "sonnet" || buckets[3].Scope != "claude-fable-5" {
		t.Fatalf("Claude = %#v, %v", buckets, err)
	}
	// A weekly primary and monthly secondary must never be confused with 5h/7d.
	codex := []byte(`{"rate_limit":{"primary_window":{"used_percent":30,"limit_window_seconds":604800,"reset_at":1800000300},"secondary_window":{"used_percent":100,"limit_window_seconds":2592000,"reset_at":1800000400}},"code_review_rate_limit":{"primary_window":{"used_percent":100,"limit_window_seconds":604800}},"additional_rate_limits":[{"limit_name":"GPT-5.3-Codex-Spark","rate_limit":{"primary_window":{"used_percent":100,"limit_window_seconds":18000,"reset_after_seconds":60}}}]}`)
	buckets, err = parseWeeklyQuota("codex", codex, now)
	if err != nil || len(buckets) != 2 || buckets[0].Window != "weekly" || buckets[0].RemainingPercent != 70 || buckets[1].Scope != "gpt-5.3-codex-spark" || !buckets[1].ResetAt.Equal(now.Add(time.Minute)) {
		t.Fatalf("Codex = %#v, %v", buckets, err)
	}
	if _, err = parseWeeklyQuota("codex", []byte(`{"rate_limit":{"primary_window":{"used_percent":2,"limit_window_seconds":2592000}}}`), now); err == nil {
		t.Fatal("monthly-only response should be unknown")
	}
	if _, err = parseWeeklyQuota("claude", []byte(`{"seven_day":{"resets_at":"2026-10-03T22:00:00Z"}}`), now); err == nil {
		t.Fatal("missing utilization must not fabricate 100% balance")
	}
	aliases := []byte(`{"rateLimit":{"allowed":false,"limitReached":true,"primaryWindow":{"limitWindowSeconds":604800,"resetAfterSeconds":9223372036854775807}},"additionalRateLimits":{"GPT-5.3-Codex-Spark":{"limitReached":true}}}`)
	buckets, err = parseWeeklyQuota("codex", aliases, now)
	if err != nil || len(buckets) != 2 || buckets[0].RemainingPercent != 0 || buckets[0].Window != "capacity" || buckets[0].ResetAt != nil || buckets[1].Scope != "gpt-5.3-codex-spark" {
		t.Fatalf("explicit denied aliases = %#v, %v", buckets, err)
	}
}

type weeklyHTTPExecutor struct {
	*replaceAwareExecutor
	request func(context.Context, *Auth, *http.Request) (*http.Response, error)
}

func (e *weeklyHTTPExecutor) HttpRequest(ctx context.Context, a *Auth, r *http.Request) (*http.Response, error) {
	return e.request(ctx, a, r)
}

func TestWeeklyQuotaRefreshUsesExecutorRetainsLastGoodAndSafeStatus(t *testing.T) {
	now := time.Date(2026, 10, 2, 22, 0, 0, 0, time.UTC)
	var fail atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/backend-api/wham/usage" || r.Header.Get("Chatgpt-Account-Id") != "account-test" {
			t.Errorf("bad usage request %s", r.URL.Path)
		}
		if fail.Load() {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = fmt.Fprint(w, `{"rate_limit":{"primary_window":{"used_percent":75,"limit_window_seconds":604800,"reset_at":1791147600}}}`)
	}))
	defer server.Close()
	manager := NewManager(nil, &WeeklyResetFirstSelector{}, nil)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	executor := &weeklyHTTPExecutor{replaceAwareExecutor: &replaceAwareExecutor{id: "codex"}, request: func(ctx context.Context, a *Auth, r *http.Request) (*http.Response, error) {
		if r.URL.Host != "chatgpt.com" {
			t.Error("usage host must be canonical")
		}
		u, _ := url.Parse(server.URL)
		r.URL.Scheme, r.URL.Host = u.Scheme, u.Host
		return server.Client().Do(r.WithContext(ctx))
	}}
	manager.RegisterExecutor(executor)
	auth, err := manager.Register(context.Background(), &Auth{ID: "private-person@example.test.json", Provider: "codex", Metadata: map[string]any{"access_token": "secret-test-token", "account_id": "account-test"}})
	if err != nil {
		t.Fatal(err)
	}
	manager.refreshWeeklyQuota(context.Background(), auth)
	if o, ok := manager.weeklyQuota.observation(auth); !ok || o.Buckets[0].RemainingPercent != 25 {
		t.Fatalf("observation = %#v/%v", o, ok)
	}
	fail.Store(true)
	now = now.Add(time.Minute)
	manager.refreshWeeklyQuota(context.Background(), auth)
	if o, _ := manager.weeklyQuota.observation(auth); o.Buckets[0].RemainingPercent != 25 || !o.ObservedAt.Equal(now.Add(-time.Minute)) {
		t.Fatal("failed probe replaced last good observation")
	}
	data, _ := json.Marshal(manager.WeeklyQuotaStatus())
	for _, secret := range []string{auth.ID, "secret-test-token", "account-test"} {
		if strings.Contains(string(data), secret) {
			t.Fatalf("unsafe status field %s", secret)
		}
	}
	if !strings.Contains(string(data), "-07:00") {
		t.Fatalf("timestamps must be Pacific: %s", data)
	}
}

func TestWeeklyQuotaDueAndLifecycleCancellation(t *testing.T) {
	now := time.Now()
	reset := now.Add(time.Minute)
	o := weeklyQuotaObservation{LastAttempt: now, ObservedAt: now, Buckets: []WeeklyQuotaBucket{{ResetAt: &reset}}}
	if weeklyQuotaDue(o, now.Add(30*time.Second)) || !weeklyQuotaDue(o, reset) || !weeklyQuotaDue(o, now.Add(weeklyQuotaRefreshInterval)) {
		t.Fatal("refresh schedule did not honor reset and interval")
	}
	manager := NewManager(nil, &WeeklyResetFirstSelector{}, nil)
	started := make(chan struct{}, 4)
	manager.RegisterExecutor(&weeklyHTTPExecutor{replaceAwareExecutor: &replaceAwareExecutor{id: "claude"}, request: func(ctx context.Context, a *Auth, r *http.Request) (*http.Response, error) {
		started <- struct{}{}
		<-ctx.Done()
		return nil, ctx.Err()
	}})
	for i := 0; i < 4; i++ {
		if _, err := manager.Register(context.Background(), &Auth{ID: fmt.Sprintf("a%d", i), Provider: "claude", Metadata: map[string]any{"access_token": "test"}}); err != nil {
			t.Fatal(err)
		}
	}
	manager.StartWeeklyQuotaRefresh(context.Background())
	for i := 0; i < 4; i++ {
		select {
		case <-started:
		case <-time.After(5 * time.Second):
			t.Fatal("initial polling did not start")
		}
	}
	stopped := make(chan struct{})
	go func() { manager.StopWeeklyQuotaRefresh(); close(stopped) }()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("polling did not cancel in-flight requests")
	}
	manager.StopWeeklyQuotaRefresh()
	if len(manager.weeklyQuota.inFlight) != 0 {
		t.Fatal("in-flight state leaked")
	}
}
