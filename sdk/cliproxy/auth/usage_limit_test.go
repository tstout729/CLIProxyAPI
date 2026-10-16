package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func usageMessage(t *testing.T, err error) string {
	t.Helper()
	if err == nil {
		t.Fatal("expected selection error")
	}
	var payload struct {
		Error struct {
			Message string `json:"message"`
			Code    string `json:"code"`
		} `json:"error"`
	}
	if errJSON := json.Unmarshal([]byte(err.Error()), &payload); errJSON != nil {
		t.Fatalf("decode error %v: %v", err, errJSON)
	}
	if payload.Error.Code != "model_cooldown" {
		t.Fatalf("error code = %q", payload.Error.Code)
	}
	return payload.Error.Message
}

func requireUsageText(t *testing.T, message string, fragments ...string) {
	t.Helper()
	for _, fragment := range fragments {
		if !strings.Contains(message, fragment) {
			t.Fatalf("message lacks %q: %s", fragment, message)
		}
	}
}

func TestUsageLimitBreakdownAndRecovery(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	for _, test := range []struct {
		name    string
		windows [][]string
		weekly  int
		five    int
	}{
		{"all weekly", [][]string{{"weekly"}, {"weekly"}}, 2, 0},
		{"all five-hour", [][]string{{"five-hour"}, {"five-hour"}}, 0, 2},
		{"three weekly one five-hour", [][]string{{"weekly"}, {"weekly"}, {"weekly"}, {"five-hour"}}, 3, 1},
		{"overlapping windows", [][]string{{"five-hour", "weekly"}, {"weekly"}}, 2, 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			cache := weeklyTestCache(now)
			selector := &WeeklyResetFirstSelector{Cache: cache}
			auths := make([]*Auth, len(test.windows))
			earliest := now.Add(2 * time.Hour)
			for i, windows := range test.windows {
				auths[i] = &Auth{ID: fmt.Sprintf("private-account-%d", i), Label: "private@example.test", FileName: "private-account.json"}
				for _, window := range windows {
					reset := earliest.Add(time.Duration(i) * time.Hour)
					if len(windows) > 1 && window == "five-hour" {
						reset = now.Add(time.Hour)
					}
					o := cache.observations[auths[i].ID]
					o.ObservedAt = now
					o.Buckets = append(o.Buckets, weeklyTestBucket(window, reset, 0))
					cache.observations[auths[i].ID] = o
				}
			}
			_, err := selector.Pick(context.Background(), "claude", "claude-sonnet-4.5", cliproxyexecutor.Options{}, auths)
			message := usageMessage(t, err)
			requireUsageText(t, message,
				fmt.Sprintf("All %d accounts considered for this request have exhausted observed usage", len(auths)),
				fmt.Sprintf("weekly %d of %d; five-hour %d of %d (counts may overlap)", test.weekly, len(auths), test.five, len(auths)),
				"Account 1:", "Account 2:", "Earliest known reset: Oct 16, 2026 2:00 AM PDT.")
			if len(test.windows[0]) > 1 {
				requireUsageText(t, message, "five-hour limit, reset Oct 16, 2026 1:00 AM PDT", "weekly limit, reset Oct 16, 2026 2:00 AM PDT; account recovery Oct 16, 2026 2:00 AM PDT")
			}
			for _, private := range []string{"private-account", "private@example.test", ".json"} {
				if strings.Contains(err.Error(), private) {
					t.Fatalf("credential identity leaked: %s", err)
				}
			}
			var cooldown *modelCooldownError
			if !errors.As(err, &cooldown) || cooldown.StatusCode() != http.StatusTooManyRequests || !cooldown.usageRecoveryAt.Equal(earliest) {
				t.Fatalf("wrong cooldown contract: %v", err)
			}
			headers := SafeResponseHeaders(err)
			if headers.Get("Retry-After") != "7200" || headers.Get(HeaderAnthropicRatelimitUnifiedReset) != strconv.FormatInt(earliest.Unix(), 10) {
				t.Fatalf("wrong recovery headers: %v", headers)
			}
			if test.name == "three weekly one five-hour" {
				t.Log(message)
			}
		})
	}
}

func TestUsageLimitUnknownResetAndImmutableEvidence(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	unknown, known := &Auth{ID: "unknown"}, &Auth{ID: "known"}
	reset := now.Add(2 * time.Hour)
	weeklyTestObserveBuckets(cache, unknown, now,
		WeeklyQuotaBucket{Scope: "all", Window: "untrusted-window@example.test", RemainingPercent: 0},
		weeklyTestBucket("weekly", now.Add(time.Hour), 0))
	weeklyTestObserveBuckets(cache, known, now, weeklyTestBucket("five-hour", reset, 0))
	calls := 0
	selector := &WeeklyResetFirstSelector{Cache: cache, modelForAuth: func(_ *Auth, model string) string {
		calls++
		return model
	}}
	_, err := selector.Pick(context.Background(), "claude", "claude-opus-4.5", cliproxyexecutor.Options{}, []*Auth{unknown, known})
	if calls != 2 {
		t.Fatalf("capacity exclusions evaluated %d times, want once per account", calls)
	}
	message := usageMessage(t, err)
	requireUsageText(t, message, "unknown window 1 of 2", "usage limit (window unknown), reset unknown", "account recovery unknown", "Earliest known reset: Oct 16, 2026 2:00 AM PDT")
	if strings.Contains(message, "untrusted-window") {
		t.Fatalf("unknown window leaked: %s", message)
	}
	before := err.Error()
	reset = now.Add(8 * time.Hour)
	cache.observations = nil
	if err.Error() != before {
		t.Fatal("emitted error changed after cache/reset mutation")
	}
}

func TestUsageLimitScopeVocabulary(t *testing.T) {
	now := time.Date(2026, 11, 1, 8, 0, 0, 0, time.UTC)
	for _, test := range []struct{ scope, model, want string }{
		{"all", "claude-sonnet-4.5", "account-wide"},
		{"sonnet", "claude-sonnet-4.5", "Sonnet"},
		{"opus", "claude-opus-4.5", "Opus"},
		{"fable", "claude-fable-5", "Fable"},
		{"claude-fable-5", "claude-fable-5", "Fable"},
		{"scope-secret@example.test", "scope-secret@example.test", "scope unknown"},
		{"claude-fable-secret@example.test", "claude-fable-secret@example.test", "scope unknown"},
	} {
		t.Run(test.scope, func(t *testing.T) {
			cache := weeklyTestCache(now)
			auth := &Auth{ID: "private-id", Label: "private-label", FileName: "private-file"}
			reset := now.Add(2 * time.Hour)
			weeklyTestObserveBuckets(cache, auth, now, WeeklyQuotaBucket{Scope: test.scope, Window: "weekly", ResetAt: &reset})
			_, err := (&WeeklyResetFirstSelector{Cache: cache}).Pick(context.Background(), "claude", test.model, cliproxyexecutor.Options{}, []*Auth{auth})
			message := usageMessage(t, err)
			requireUsageText(t, message, test.want+" weekly limit", "Nov 1, 2026 2:00 AM PST")
			if strings.Contains(message, "@") || strings.Contains(message, "private-") {
				t.Fatalf("untrusted scope or identity leaked: %s", message)
			}
		})
	}
}

func TestUsageLimitOldOrUnrelatedEvidenceStaysEligible(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	for _, mode := range []string{"stale", "passed reset", "epoch mismatch", "unrelated scope"} {
		t.Run(mode, func(t *testing.T) {
			cache := weeklyTestCache(now)
			eligible, exhausted := &Auth{ID: "eligible", RegistrationEpoch: 1}, &Auth{ID: "exhausted"}
			weeklyTestObserve(cache, exhausted, now, now.Add(time.Hour), 0)
			reset := now.Add(24 * time.Hour)
			o := weeklyQuotaObservation{Epoch: 1, ObservedAt: now, Buckets: []WeeklyQuotaBucket{weeklyTestBucket("weekly", reset, 0)}}
			switch mode {
			case "stale":
				o.ObservedAt = now.Add(-weeklyQuotaStaleTTL)
			case "passed reset":
				o.Buckets[0].ResetAt = &now
			case "epoch mismatch":
				o.Epoch = 2
			case "unrelated scope":
				o.Buckets[0].Scope = "opus"
			}
			cache.observations[eligible.ID] = o
			selector := &WeeklyResetFirstSelector{Cache: cache}
			got, err := selector.Pick(context.Background(), "claude", "claude-sonnet-4.5(high)", cliproxyexecutor.Options{}, []*Auth{eligible, exhausted})
			if err != nil || got != eligible {
				t.Fatalf("unknown/unrelated evidence blocked eligible account: %v, %v", got, err)
			}
			eligible.Unavailable = true
			eligible.NextRetryAfter = now.Add(time.Minute)
			eligible.Quota.Exceeded = true
			_, err = selector.Pick(context.Background(), "claude", "claude-sonnet-4.5(high)", cliproxyexecutor.Options{}, []*Auth{eligible, exhausted})
			message := usageMessage(t, err)
			requireUsageText(t, message, "Observed usage is exhausted for 1 of 2", "weekly 1 of 2", "Account 2:")
			if strings.Contains(message, "Account 1:") {
				t.Fatalf("invalid observation reported as current: %s", message)
			}
		})
	}
}

func TestUsageLimitMixedCooldownPreservesManagerContract(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	for _, affinity := range []bool{false, true} {
		t.Run(fmt.Sprintf("affinity=%v", affinity), func(t *testing.T) {
			var selector Selector = &WeeklyResetFirstSelector{}
			if affinity {
				wrapped := NewSessionAffinitySelector(selector)
				defer wrapped.Stop()
				selector = wrapped
			}
			manager := NewManager(nil, selector, nil)
			manager.weeklyQuota.nowFunc = func() time.Time { return now }
			cause := &Error{Message: "observed upstream rate limit", HTTPStatus: http.StatusTooManyRequests}
			quota := &Auth{ID: "quota", Provider: "claude"}
			cooling := &Auth{ID: "cooling", Provider: "claude", Unavailable: true, NextRetryAfter: now.Add(40 * time.Minute), Quota: QuotaState{Exceeded: true}, LastError: cause}
			weeklyTestObserve(manager.weeklyQuota, quota, now, now.Add(3*24*time.Hour), 0)
			weeklyTestObserve(manager.weeklyQuota, cooling, now, now.Add(5*24*time.Hour), 22)
			_, _, err := manager.availableAuthsForSelector(selector, []*Auth{quota, cooling}, "claude", "claude-opus-4.5", now)
			message := usageMessage(t, err)
			requireUsageText(t, message, "Observed usage is exhausted for 1 of 2", "remaining account is cooling down; its usage window is unknown", "weekly 1 of 2; five-hour 0 of 2", "observed upstream rate limit", "Next retry in 40m0s.", "Earliest known usage reset: Oct 19, 2026 12:00 AM PDT")
			var cooldown *modelCooldownError
			if !errors.As(err, &cooldown) || !errors.Is(err, cause) || cooldown.StatusCode() != 429 || cooldown.resetIn != 40*time.Minute || !cooldown.usageRecoveryAt.IsZero() {
				t.Fatalf("mixed cooldown changed contract: %v", err)
			}
			if SafeResponseHeaders(err).Get("Retry-After") != "2400" {
				t.Fatalf("mixed cooldown changed Retry-After: %v", SafeResponseHeaders(err))
			}
			var payload map[string]map[string]any
			_ = json.Unmarshal([]byte(err.Error()), &payload)
			if _, ok := payload["error"]["reset_at"]; ok {
				t.Fatal("observed usage reset replaced ordinary cooldown reset")
			}
		})
	}
}

func TestUsageLimitManagerCountsOnlyRelevantCandidates(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	manager := NewManager(nil, &WeeklyResetFirstSelector{}, nil)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	manager.RegisterExecutor(&replaceAwareExecutor{id: "claude"})
	model := "claude-sonnet-usage-relevant-test"
	for i := 0; i < 3; i++ {
		id := fmt.Sprintf("usage-relevant-%d", i)
		auth, err := manager.Register(context.Background(), &Auth{ID: id, Provider: "claude", Disabled: i == 2})
		if err != nil {
			t.Fatal(err)
		}
		registry.GetGlobalRegistry().RegisterClient(id, "claude", []*registry.ModelInfo{{ID: model}})
		t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(id) })
		weeklyTestObserve(manager.weeklyQuota, auth, now, now.Add(time.Duration(i+1)*time.Hour), 0)
	}
	_, err := manager.SelectAuth(context.Background(), "claude", model, cliproxyexecutor.Options{})
	requireUsageText(t, usageMessage(t, err), "All 2 accounts", "weekly 2 of 2")
}

func TestUsageLimitUnknownOnlyKeepsUnavailableContract(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	for _, affinity := range []bool{false, true} {
		t.Run(fmt.Sprintf("affinity=%v", affinity), func(t *testing.T) {
			cache := weeklyTestCache(now)
			var selector Selector = &WeeklyResetFirstSelector{Cache: cache}
			if affinity {
				wrapped := NewSessionAffinitySelector(selector)
				defer wrapped.Stop()
				selector = wrapped
			}
			auth := &Auth{ID: "unknown-reset"}
			weeklyTestObserveBuckets(cache, auth, now, WeeklyQuotaBucket{Scope: "all", Window: "weekly"})
			_, err := selector.Pick(context.Background(), "claude", "claude-opus-4.5", cliproxyexecutor.Options{}, []*Auth{auth})
			var base *Error
			if !errors.As(err, &base) || base.Code != "auth_not_found" || base.HTTPStatus != 0 {
				t.Fatalf("unknown reset changed existing unavailable contract: %v", err)
			}
			requireUsageText(t, err.Error(), "weekly 1 of 1", "weekly limit, reset unknown", "Earliest known reset: unknown")
			if headers := SafeResponseHeaders(err); headers.Get("Retry-After") != "" || headers.Get(HeaderAnthropicRatelimitUnifiedReset) != "" {
				t.Fatalf("fabricated unknown reset headers: %v", headers)
			}
		})
	}
}

func TestUsageLimitDisplayRoundsUpWithoutChangingResetContract(t *testing.T) {
	now := time.Date(2026, 10, 16, 7, 0, 0, 0, time.UTC)
	reset := now.Add(time.Hour - 500*time.Millisecond)
	cache := weeklyTestCache(now)
	auth := &Auth{ID: "fractional-reset"}
	weeklyTestObserve(cache, auth, now, reset, 0)
	_, err := (&WeeklyResetFirstSelector{Cache: cache}).Pick(context.Background(), "claude", "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{auth})
	requireUsageText(t, usageMessage(t, err), "reset Oct 16, 2026 1:00 AM PDT", "Earliest known reset: Oct 16, 2026 1:00 AM PDT")
	if headers := SafeResponseHeaders(err); headers.Get(HeaderAnthropicRatelimitUnifiedReset) != strconv.FormatInt(reset.Unix()+1, 10) || headers.Get("Retry-After") != "3600" {
		t.Fatalf("machine reset changed: %v", headers)
	}
	var cooldown *modelCooldownError
	if !errors.As(err, &cooldown) || !cooldown.usageRecoveryAt.Equal(reset) {
		t.Fatalf("exact observed reset changed: %v", err)
	}
}

func TestUsageLimitDirectAffinityRetainsMixedReport(t *testing.T) {
	now := time.Date(2099, 10, 16, 7, 0, 0, 0, time.UTC)
	for _, session := range []string{"", "usage-thread"} {
		t.Run("session="+session, func(t *testing.T) {
			cache := weeklyTestCache(now)
			selector := NewSessionAffinitySelector(&WeeklyResetFirstSelector{Cache: cache})
			defer selector.Stop()
			exhausted := &Auth{ID: "exhausted"}
			cooling := &Auth{ID: "cooling", Unavailable: true, Quota: QuotaState{Exceeded: true}, NextRetryAfter: now.Add(40 * time.Minute)}
			weeklyTestObserve(cache, exhausted, now, now.Add(3*24*time.Hour), 0)
			weeklyTestObserve(cache, cooling, now, now.Add(4*24*time.Hour), 25)
			_, err := selector.Pick(context.Background(), "claude", "claude-opus-4.5", cliproxyexecutor.Options{Headers: http.Header{"X-Session-Id": []string{session}}}, []*Auth{exhausted, cooling})
			requireUsageText(t, usageMessage(t, err), "Observed usage is exhausted for 1 of 2", "remaining account is cooling down; its usage window is unknown", "weekly 1 of 2; five-hour 0 of 2", "Next retry in")
		})
	}
}
