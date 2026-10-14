package auth

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func weeklyTestObserveBuckets(c *WeeklyQuotaCache, auth *Auth, now time.Time, buckets ...WeeklyQuotaBucket) {
	c.observations[auth.ID] = weeklyQuotaObservation{Epoch: auth.RegistrationEpoch, ObservedAt: now, LastAttempt: now, Buckets: buckets}
}

func weeklyTestBucket(window string, reset time.Time, remaining float64) WeeklyQuotaBucket {
	return WeeklyQuotaBucket{Scope: "all", Window: window, ResetAt: &reset, RemainingPercent: remaining}
}

// When every account's usage is exhausted, the request fails with a rate limit
// naming the first account's reset instead of "no auth candidates", without
// calling upstream. Once that reset passes, the same request succeeds.
func TestWeeklyAllExhaustedReportsFirstUsageReset(t *testing.T) {
	now := time.Now()
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{})
	defer affinity.Stop()
	manager := NewManager(nil, affinity, nil)
	manager.SetRetryConfig(3, 30*time.Second, 0)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	model := "claude-opus-weekly-exhausted-test"
	var attempts []string
	manager.RegisterExecutor(&customStreamMockExecutor{
		identifier: "claude",
		streamFn: func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
			attempts = append(attempts, selected.ID)
			chunks := make(chan cliproxyexecutor.StreamChunk, 1)
			chunks <- cliproxyexecutor.StreamChunk{Payload: []byte("data: {\"type\":\"message_start\"}\n\n")}
			close(chunks)
			return &cliproxyexecutor.StreamResult{Chunks: chunks}, nil
		},
	})
	register := func(id string) *Auth {
		t.Helper()
		registry.GetGlobalRegistry().RegisterClient(id, "claude", []*registry.ModelInfo{{ID: model}})
		t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(id) })
		auth, errRegister := manager.Register(context.Background(), &Auth{ID: id, Provider: "claude", Status: StatusActive})
		if errRegister != nil {
			t.Fatal(errRegister)
		}
		return auth
	}
	fiveHourOut, weeklyOut, bothOut := register("exhausted-five-hour"), register("exhausted-weekly"), register("exhausted-both")
	start := now
	firstReset := start.Add(31 * time.Minute)
	observe := func() {
		// Weekly balance left, but the five-hour window is spent until firstReset.
		weeklyTestObserveBuckets(manager.weeklyQuota, fiveHourOut, now,
			weeklyTestBucket("five-hour", firstReset, 0), weeklyTestBucket("weekly", start.Add(5*24*time.Hour), 22))
		weeklyTestObserveBuckets(manager.weeklyQuota, weeklyOut, now,
			weeklyTestBucket("five-hour", start.Add(3*time.Hour), 64), weeklyTestBucket("weekly", start.Add(3*24*time.Hour), 0))
		// An account recovers only when every spent window has reset: its earlier
		// five-hour reset must not be reported or make it eligible.
		weeklyTestObserveBuckets(manager.weeklyQuota, bothOut, now,
			weeklyTestBucket("five-hour", start.Add(10*time.Minute), 0), weeklyTestBucket("weekly", start.Add(4*24*time.Hour), 0))
	}
	observe()

	opts := cliproxyexecutor.Options{Stream: true, Headers: http.Header{"X-Session-Id": []string{"weekly-exhausted-thread"}}}
	execute := func() (*cliproxyexecutor.StreamResult, error) {
		return manager.ExecuteStream(context.Background(), []string{"claude"}, cliproxyexecutor.Request{Model: model}, opts)
	}
	_, errStream := execute()
	var cooldown *modelCooldownError
	if !errors.As(errStream, &cooldown) || cooldown == nil {
		t.Fatalf("ExecuteStream error = %v, want usage exhaustion", errStream)
	}
	if status := cooldown.StatusCode(); status != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", status)
	}
	headers := SafeResponseHeaders(errStream)
	if got := headers.Get("Retry-After"); got != "1860" {
		t.Fatalf("Retry-After = %q, want 1860", got)
	}
	wantReset := firstReset.Unix()
	if firstReset.Nanosecond() > 0 {
		wantReset++
	}
	if got := headers.Get(HeaderAnthropicRatelimitUnifiedReset); got != strconv.FormatInt(wantReset, 10) {
		t.Fatalf("%s = %q, want %d", HeaderAnthropicRatelimitUnifiedReset, got, wantReset)
	}
	if !strings.Contains(errStream.Error(), "out of usage until") {
		t.Fatalf("message = %s", errStream.Error())
	}
	if len(attempts) != 0 {
		t.Fatalf("upstream called while exhausted: %v", attempts)
	}

	// The selector without affinity reports the same reset.
	weekly := &WeeklyResetFirstSelector{Cache: manager.weeklyQuota}
	_, errPick := weekly.Pick(context.Background(), "claude", model, cliproxyexecutor.Options{}, []*Auth{weeklyOut, bothOut, fiveHourOut})
	if !errors.As(errPick, &cooldown) || !cooldown.usageResetAt.Equal(firstReset) {
		t.Fatalf("weekly Pick error = %v, want reset at %s", errPick, firstReset)
	}

	// The next usage poll sees the first reset pass.
	now = firstReset.Add(time.Second)
	observe()
	result, errStream := execute()
	if errStream != nil {
		t.Fatalf("ExecuteStream after reset error = %v", errStream)
	}
	for range result.Chunks {
	}
	if len(attempts) != 1 || attempts[0] != fiveHourOut.ID {
		t.Fatalf("attempts after reset = %v, want [%s]", attempts, fiveHourOut.ID)
	}
}

// The last open account hits its limit upstream before the next usage poll,
// while every other account is usage-exhausted for days. The client must wait
// for that account's own reset, not for the other accounts' weekly resets.
func TestWeeklyLastAccountUpstreamLimitReportsItsOwnReset(t *testing.T) {
	withQuotaCooldownEnabled(t)
	now := time.Now()
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{})
	defer affinity.Stop()
	manager := NewManager(nil, affinity, nil)
	manager.SetRetryConfig(3, 30*time.Second, 0)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	model := "claude-opus-weekly-last-account-test"
	upstreamReset := 40 * time.Minute
	var attempts []string
	manager.RegisterExecutor(&customStreamMockExecutor{
		identifier: "claude",
		streamFn: func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
			attempts = append(attempts, selected.ID)
			return nil, customStatusError{code: http.StatusTooManyRequests, msg: `{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account's rate limit."}}`, retryAfter: &upstreamReset}
		},
	})
	var open *Auth
	for _, id := range []string{"last-open", "last-weekly-out-a", "last-weekly-out-b"} {
		registry.GetGlobalRegistry().RegisterClient(id, "claude", []*registry.ModelInfo{{ID: model}})
		t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(id) })
		auth, errRegister := manager.Register(context.Background(), &Auth{ID: id, Provider: "claude", Status: StatusActive})
		if errRegister != nil {
			t.Fatal(errRegister)
		}
		if open == nil {
			open = auth
			weeklyTestObserve(manager.weeklyQuota, auth, now, now.Add(5*24*time.Hour), 22)
			continue
		}
		weeklyTestObserve(manager.weeklyQuota, auth, now, now.Add(3*24*time.Hour), 0)
	}

	_, errStream := manager.ExecuteStream(context.Background(), []string{"claude"}, cliproxyexecutor.Request{Model: model}, cliproxyexecutor.Options{Stream: true})
	if errStream == nil {
		t.Fatal("ExecuteStream succeeded, want rate limit")
	}
	if len(attempts) == 0 || attempts[0] != open.ID {
		t.Fatalf("attempts = %v, want %s first", attempts, open.ID)
	}
	headers := SafeResponseHeaders(errStream)
	retryAfter, err := strconv.Atoi(headers.Get("Retry-After"))
	if err != nil || time.Duration(retryAfter)*time.Second > upstreamReset+time.Minute {
		t.Fatalf("Retry-After = %q (%T: %v), want about %s", headers.Get("Retry-After"), errStream, errStream, upstreamReset)
	}
	reset, err := strconv.ParseInt(headers.Get(HeaderAnthropicRatelimitUnifiedReset), 10, 64)
	if wait := time.Until(time.Unix(reset, 0)); err != nil || wait > upstreamReset+time.Minute {
		t.Fatalf("%s = %q, want about %s from now", HeaderAnthropicRatelimitUnifiedReset, headers.Get(HeaderAnthropicRatelimitUnifiedReset), upstreamReset)
	}
}

// Without any known reset, exhaustion keeps the previous unavailable error.
func TestWeeklyAllExhaustedWithoutResetKeepsUnavailableError(t *testing.T) {
	now := time.Now()
	cache := weeklyTestCache(now)
	selector := &WeeklyResetFirstSelector{Cache: cache}
	auth := &Auth{ID: "exhausted-no-reset"}
	weeklyTestObserveBuckets(cache, auth, now, WeeklyQuotaBucket{Scope: "all", Window: "weekly", RemainingPercent: 0})
	_, err := selector.Pick(context.Background(), "claude", "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{auth})
	var cooldown *modelCooldownError
	if err == nil || errors.As(err, &cooldown) {
		t.Fatalf("Pick error = %v, want unavailable without a reset", err)
	}
}
