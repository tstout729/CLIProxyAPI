package auth

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strings"
	"testing"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func weeklyReservePick(t *testing.T, selector Selector, model string, opts cliproxyexecutor.Options, auths []*Auth, want string) {
	t.Helper()
	got, err := selector.Pick(context.Background(), "claude", model, opts, auths)
	if err != nil || got == nil || got.ID != want {
		t.Fatalf("Pick = %v, %v; want %s", got, err, want)
	}
}

func TestWeeklyReserveZeroKeepsResetOrdering(t *testing.T) {
	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	early, later := &Auth{ID: "z-early"}, &Auth{ID: "a-later"}
	weeklyTestObserve(cache, early, now, now.Add(time.Hour), 1)
	weeklyTestObserve(cache, later, now, now.Add(24*time.Hour), 90)
	// Zero, negative, and non-finite reserves all mean today's behavior.
	for _, reserve := range []float64{0, -5, math.NaN(), math.Inf(-1)} {
		selector := &WeeklyResetFirstSelector{Cache: cache, ReservePercent: reserve}
		weeklyReservePick(t, selector, "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{later, early}, early.ID)
		if got := selector.filter([]*Auth{later, early}, "claude-sonnet-4.5", now); len(got.auths) != 2 {
			t.Fatalf("reserve %v filtered %d accounts", reserve, 2-len(got.auths))
		}
	}
}

func TestWeeklyReserveSkipsReservedWhileAnotherIsAbove(t *testing.T) {
	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	selector := &WeeklyResetFirstSelector{Cache: cache, ReservePercent: 15}
	drained, healthy, unknown := &Auth{ID: "a-drained"}, &Auth{ID: "b-healthy"}, &Auth{ID: "c-unknown"}
	pick := func(auths []*Auth, want string) {
		t.Helper()
		weeklyReservePick(t, selector, "claude-sonnet-4.5", cliproxyexecutor.Options{}, auths, want)
	}
	// Exactly at the reserve counts as reserved, even with the earliest reset.
	weeklyTestObserve(cache, drained, now, now.Add(time.Hour), 15)
	weeklyTestObserve(cache, healthy, now, now.Add(48*time.Hour), 60)
	pick([]*Auth{drained, healthy, unknown}, healthy.ID)
	// Unknown capacity is never reserved: it still ranks after known
	// unreserved capacity, but it is used before a reserved account.
	pick([]*Auth{drained, unknown}, unknown.ID)
	// Just above the reserve is unreserved and keeps reset-first ordering.
	weeklyTestObserve(cache, drained, now, now.Add(time.Hour), 15.5)
	pick([]*Auth{drained, healthy, unknown}, drained.ID)
	// Exhausted stays excluded, with or without a reserve.
	weeklyTestObserve(cache, drained, now, now.Add(time.Hour), 0)
	pick([]*Auth{drained, unknown}, unknown.ID)
	if got, err := selector.Pick(context.Background(), "claude", "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{drained}); err == nil {
		t.Fatalf("exhausted account was selected: %v", got)
	}
}

func TestWeeklyReserveAllReservedFallsBackToNormalRanking(t *testing.T) {
	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	selector := &WeeklyResetFirstSelector{Cache: cache, ReservePercent: 15}
	soon, later, open, empty := &Auth{ID: "z-soon"}, &Auth{ID: "a-later"}, &Auth{ID: "m-open"}, &Auth{ID: "b-empty"}
	weeklyTestObserve(cache, soon, now, now.Add(time.Hour), 10)
	weeklyTestObserve(cache, later, now, now.Add(24*time.Hour), 12)
	weeklyTestObserve(cache, open, now, now.Add(48*time.Hour), 90)
	weeklyTestObserve(cache, empty, now, now.Add(30*time.Minute), 0)
	pick := func(auths []*Auth, want string) {
		t.Helper()
		weeklyReservePick(t, selector, "claude-sonnet-4.5", cliproxyexecutor.Options{}, auths, want)
	}
	// Every eligible account is reserved: earliest reset wins; exhausted stays out.
	pick([]*Auth{later, soon, empty}, soon.ID)
	// An unreserved account the request cannot use does not strand the request.
	open.Unavailable, open.NextRetryAfter = true, now.Add(time.Minute)
	pick([]*Auth{later, soon, open}, soon.ID)
	open.Unavailable, open.NextRetryAfter = false, time.Time{}
	open.Disabled = true
	pick([]*Auth{later, soon, open}, soon.ID)
	open.Disabled = false
	pick([]*Auth{later, soon, open}, open.ID)
	// A retry that already tried the unreserved account falls back to reserved ones.
	pick([]*Auth{later, soon}, soon.ID)
}

func TestWeeklyReserveUsesApplicableWeeklyBuckets(t *testing.T) {
	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	selector := &WeeklyResetFirstSelector{Cache: cache, ReservePercent: 15}
	scoped, other := &Auth{ID: "a-scoped"}, &Auth{ID: "b-other"}
	reset := now.Add(time.Hour)
	weeklyTestObserve(cache, scoped, now, reset, 80)
	o := cache.observations[scoped.ID]
	o.Buckets = append(o.Buckets,
		WeeklyQuotaBucket{Scope: "opus", Window: "weekly", ResetAt: &reset, RemainingPercent: 10},
		WeeklyQuotaBucket{Scope: "all", Window: "five-hour", ResetAt: &reset, RemainingPercent: 5},
	)
	cache.observations[scoped.ID] = o
	weeklyTestObserve(cache, other, now, now.Add(24*time.Hour), 90)
	// Five-hour capacity is not a weekly reserve; the Opus bucket does not apply to Sonnet.
	weeklyReservePick(t, selector, "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{scoped, other}, scoped.ID)
	// The model-scoped weekly bucket reserves the account for that model.
	weeklyReservePick(t, selector, "claude-opus-4.5", cliproxyexecutor.Options{}, []*Auth{scoped, other}, other.ID)
	// A passed reset makes capacity unknown, which is never reserved.
	if capacity := cache.capacity(scoped, "claude-opus-4.5", reset); selector.reserved(scoped, capacity) {
		t.Fatal("unknown capacity after a passed reset was reserved")
	}
	// Stale observations are unknown too.
	if capacity := cache.capacity(scoped, "claude-opus-4.5", now.Add(weeklyQuotaStaleTTL)); selector.reserved(scoped, capacity) {
		t.Fatal("stale capacity was reserved")
	}
}

func TestWeeklyReservePerAccountOverride(t *testing.T) {
	selector := &WeeklyResetFirstSelector{ReservePercent: 15}
	for _, tc := range []struct {
		name       string
		attributes map[string]string
		metadata   map[string]any
		want       float64
	}{
		{name: "default", want: 15},
		{name: "metadata number", metadata: map[string]any{AttributeWeeklyReservePercent: float64(30)}, want: 30},
		{name: "metadata int", metadata: map[string]any{AttributeWeeklyReservePercent: 40}, want: 40},
		{name: "metadata json number", metadata: map[string]any{AttributeWeeklyReservePercent: json.Number("25.5")}, want: 25.5},
		{name: "metadata string", metadata: map[string]any{AttributeWeeklyReservePercent: " 35 "}, want: 35},
		{name: "explicit zero opts out", metadata: map[string]any{AttributeWeeklyReservePercent: float64(0)}, want: 0},
		{name: "attribute beats metadata", attributes: map[string]string{AttributeWeeklyReservePercent: "20"}, metadata: map[string]any{AttributeWeeklyReservePercent: float64(30)}, want: 20},
		{name: "above 100 ignored", metadata: map[string]any{AttributeWeeklyReservePercent: float64(101)}, want: 15},
		{name: "negative ignored", metadata: map[string]any{AttributeWeeklyReservePercent: float64(-1)}, want: 15},
		{name: "text ignored", metadata: map[string]any{AttributeWeeklyReservePercent: "half"}, want: 15},
		{name: "bool ignored", metadata: map[string]any{AttributeWeeklyReservePercent: true}, want: 15},
	} {
		auth := &Auth{ID: "override", Attributes: tc.attributes, Metadata: tc.metadata}
		if got := selector.reserveFor(auth); got != tc.want {
			t.Errorf("%s: reserveFor = %v, want %v", tc.name, got, tc.want)
		}
	}

	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	admin := &Auth{ID: "a-admin", Metadata: map[string]any{AttributeWeeklyReservePercent: float64(30)}}
	other := &Auth{ID: "b-other"}
	weeklyTestObserve(cache, admin, now, now.Add(time.Hour), 25)
	weeklyTestObserve(cache, other, now, now.Add(24*time.Hour), 5)
	// The override alone reserves one account while the global reserve is off.
	weeklyReservePick(t, &WeeklyResetFirstSelector{Cache: cache}, "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{admin, other}, other.ID)
	// A per-account 0 opts out of the global reserve.
	admin.Metadata[AttributeWeeklyReservePercent] = float64(0)
	weeklyTestObserve(cache, admin, now, now.Add(time.Hour), 10)
	weeklyTestObserve(cache, other, now, now.Add(24*time.Hour), 90)
	weeklyReservePick(t, &WeeklyResetFirstSelector{Cache: cache, ReservePercent: 15}, "claude-sonnet-4.5", cliproxyexecutor.Options{}, []*Auth{admin, other}, admin.ID)
}

func TestWeeklyReserveAffinityRebindsOffReservedAccount(t *testing.T) {
	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	cache := weeklyTestCache(now)
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{Cache: cache, ReservePercent: 15})
	defer affinity.Stop()
	a, b := &Auth{ID: "a"}, &Auth{ID: "b"}
	weeklyTestObserve(cache, a, now, now.Add(time.Hour), 40)
	weeklyTestObserve(cache, b, now, now.Add(24*time.Hour), 90)
	opts := cliproxyexecutor.Options{Headers: http.Header{"X-Session-Id": []string{"reserve-thread"}}}
	pick := func(want string) {
		t.Helper()
		weeklyReservePick(t, affinity, "claude-sonnet-4.5", opts, []*Auth{a, b}, want)
	}
	pick(a.ID)
	// The bound account drains into its reserve: the next request rebinds.
	weeklyTestObserve(cache, a, now, now.Add(time.Hour), 12)
	pick(b.ID)
	if got, status := affinity.LookupAffinity("claude", "claude-sonnet-4.5", "reserve-thread"); got != b.ID || status != "bound" {
		t.Fatalf("Lookup = %s/%s, want rebinding to %s", got, status, b.ID)
	}
	// The reserve never strands a thread: with the unreserved account
	// unavailable, the reserved account serves it.
	b.Unavailable = true
	pick(a.ID)
	// Once an unreserved account is usable again, the thread moves off the reserve.
	b.Unavailable = false
	pick(b.ID)
	// A thread already on an unreserved account stays there.
	weeklyTestObserve(cache, a, now, now.Add(time.Hour), 70)
	pick(b.ID)
}

func TestWeeklyReserveManagerAppliesBeforePriorityAndAffinity(t *testing.T) {
	now := time.Now()
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{ReservePercent: 15})
	defer affinity.Stop()
	manager := NewManager(nil, affinity, nil)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	manager.RegisterExecutor(&replaceAwareExecutor{id: "claude"})
	high, _ := manager.Register(context.Background(), &Auth{ID: "weekly-reserve-high", Provider: "claude", Attributes: map[string]string{"priority": "9"}})
	low, _ := manager.Register(context.Background(), &Auth{ID: "weekly-reserve-low", Provider: "claude"})
	threadOpts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.DerivedSessionIDMetadataKey: "weekly-reserve-thread"}}
	pick := func(opts cliproxyexecutor.Options, want string) {
		t.Helper()
		got, err := manager.SelectAuth(context.Background(), "claude", "", opts)
		if err != nil || got == nil || got.ID != want {
			t.Fatalf("SelectAuth = %v, %v; want %s", got, err, want)
		}
	}
	// A reserved higher-priority account yields to an unreserved lower-priority one.
	weeklyTestObserve(manager.weeklyQuota, high, now, now.Add(time.Hour), 10)
	weeklyTestObserve(manager.weeklyQuota, low, now, now.Add(24*time.Hour), 80)
	pick(cliproxyexecutor.Options{}, low.ID)
	pick(threadOpts, low.ID)
	// The bound account drains into its reserve after the other's week resets.
	weeklyTestObserve(manager.weeklyQuota, high, now, now.Add(7*24*time.Hour), 95)
	weeklyTestObserve(manager.weeklyQuota, low, now, now.Add(24*time.Hour), 12)
	pick(threadOpts, high.ID)
	// With every account reserved, normal ranking (priority first) applies.
	weeklyTestObserve(manager.weeklyQuota, high, now, now.Add(7*24*time.Hour), 10)
	pick(threadOpts, high.ID)
	pick(cliproxyexecutor.Options{}, high.ID)
}

func TestWeeklyReserveStatus(t *testing.T) {
	now := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	affinity := NewSessionAffinitySelector(&WeeklyResetFirstSelector{ReservePercent: 15})
	defer affinity.Stop()
	manager := NewManager(nil, affinity, nil)
	manager.weeklyQuota.nowFunc = func() time.Time { return now }
	if got := manager.WeeklyReservePercent(); got != 15 {
		t.Fatalf("WeeklyReservePercent = %v", got)
	}
	register := func(id string, metadata map[string]any) *Auth {
		auth, err := manager.Register(context.Background(), &Auth{ID: id, Provider: "claude", Metadata: metadata})
		if err != nil {
			t.Fatal(err)
		}
		return auth
	}
	drained := register("weekly-reserve-status-drained", nil)
	healthy := register("weekly-reserve-status-healthy", nil)
	override := register("weekly-reserve-status-override", map[string]any{AttributeWeeklyReservePercent: float64(70)})
	unknown := register("weekly-reserve-status-unknown", nil)
	weeklyTestObserve(manager.weeklyQuota, drained, now, now.Add(time.Hour), 9)
	weeklyTestObserve(manager.weeklyQuota, healthy, now, now.Add(time.Hour), 60)
	weeklyTestObserve(manager.weeklyQuota, override, now, now.Add(time.Hour), 60)
	want := map[string]struct {
		reserve  float64
		reserved bool
		status   string
	}{
		drained.EnsureIndex():  {15, true, "ready"},
		healthy.EnsureIndex():  {15, false, "ready"},
		override.EnsureIndex(): {70, true, "ready"},
		unknown.EnsureIndex():  {15, false, "unknown"},
	}
	rows := manager.WeeklyQuotaStatus()
	if len(rows) != len(want) {
		t.Fatalf("rows = %#v", rows)
	}
	for _, row := range rows {
		expected, ok := want[row.AuthIndex]
		if !ok || row.ReservePercent != expected.reserve || row.Reserved != expected.reserved || row.Status != expected.status {
			t.Errorf("row %s = reserve %v reserved %v status %s, want %+v", row.AuthIndex, row.ReservePercent, row.Reserved, row.Status, expected)
		}
	}
	data, _ := json.Marshal(rows)
	if !strings.Contains(string(data), `"reserve_percent":15`) || !strings.Contains(string(data), `"reserved":true`) {
		t.Fatalf("status JSON missing reserve fields: %s", data)
	}
	manager.SetSelector(&RoundRobinSelector{})
	if got := manager.WeeklyReservePercent(); got != 0 {
		t.Fatalf("non-weekly strategy reports reserve %v", got)
	}
}
