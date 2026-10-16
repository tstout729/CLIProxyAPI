package auth

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	"github.com/tidwall/gjson"
)

const (
	weeklyQuotaRefreshInterval = 5 * time.Minute
	weeklyQuotaStaleTTL        = 15 * time.Minute
	weeklyQuotaScanInterval    = 15 * time.Second
)

// WeeklyQuotaBucket is a provider-observed capacity window. Scope is "all",
// a Claude model family, or a normalized Codex model name.
type WeeklyQuotaBucket struct {
	Scope            string     `json:"scope"`
	Window           string     `json:"window"`
	ResetAt          *time.Time `json:"reset_at,omitempty"`
	RemainingPercent float64    `json:"remaining_percent"`
}

type weeklyQuotaObservation struct {
	Epoch       uint64
	ObservedAt  time.Time
	LastAttempt time.Time
	Buckets     []WeeklyQuotaBucket
}

// WeeklyQuotaCache retains last-good observations independently of cooldown
// state and auth persistence. It never stores credentials or customer identity.
type WeeklyQuotaCache struct {
	mu           sync.RWMutex
	observations map[string]weeklyQuotaObservation
	inFlight     map[string]uint64
	nowFunc      func() time.Time
}

func newWeeklyQuotaCache() *WeeklyQuotaCache {
	return &WeeklyQuotaCache{observations: make(map[string]weeklyQuotaObservation), inFlight: make(map[string]uint64), nowFunc: time.Now}
}

func (c *WeeklyQuotaCache) observation(auth *Auth) (weeklyQuotaObservation, bool) {
	if c == nil || auth == nil {
		return weeklyQuotaObservation{}, false
	}
	c.mu.RLock()
	o, ok := c.observations[auth.ID]
	c.mu.RUnlock()
	return o, ok && o.Epoch == auth.RegistrationEpoch && !o.ObservedAt.IsZero()
}

func weeklyBucketApplies(scope, model string) bool {
	model = strings.ToLower(canonicalModelKey(model))
	switch scope {
	case "all":
		return true
	case "sonnet":
		return strings.Contains(model, "sonnet")
	case "opus":
		return strings.Contains(model, "opus")
	case "fable":
		return strings.Contains(model, "fable")
	default:
		return model != "" && model == scope
	}
}

// weeklyCapacity summarizes current observed capacity for one model. The zero
// value means unknown: no observation, a stale one, or a passed reset.
type weeklyCapacity struct {
	reset     time.Time // earliest upcoming weekly reset
	weekly    bool      // at least one current weekly bucket applies
	remaining float64   // lowest applicable weekly RemainingPercent (0-100)
	exhausted bool
	recoverAt time.Time // when every exhausted bucket has reset; zero if unknown
	blockers  []usageBlocker
}

// capacity reports the applicable buckets for model. Passed resets become
// unknown until a new upstream observation; crossing a reset never invents a
// replenished balance.
func (c *WeeklyQuotaCache) capacity(auth *Auth, model string, now time.Time) weeklyCapacity {
	o, ok := c.observation(auth)
	if !ok || now.Sub(o.ObservedAt) >= weeklyQuotaStaleTTL {
		return weeklyCapacity{}
	}
	var result weeklyCapacity
	unknownAfterReset := false
	exhausted := weeklyCapacity{}
	recoverUnknown := false
	for _, bucket := range o.Buckets {
		if !weeklyBucketApplies(bucket.Scope, model) {
			continue
		}
		if bucket.ResetAt != nil && !bucket.ResetAt.After(now) {
			unknownAfterReset = true
			continue
		}
		if bucket.RemainingPercent <= 0 {
			exhausted.exhausted = true
			exhausted.blockers = append(exhausted.blockers, usageBlockerFromBucket(bucket))
			if bucket.ResetAt == nil {
				recoverUnknown = true
			} else if bucket.ResetAt.After(exhausted.recoverAt) {
				exhausted.recoverAt = *bucket.ResetAt
			}
			continue
		}
		if bucket.Window != "weekly" {
			continue
		}
		if !result.weekly || bucket.RemainingPercent < result.remaining {
			result.remaining = bucket.RemainingPercent
		}
		result.weekly = true
		if bucket.ResetAt != nil && (result.reset.IsZero() || bucket.ResetAt.Before(result.reset)) {
			result.reset = *bucket.ResetAt
		}
	}
	if exhausted.exhausted {
		if recoverUnknown {
			exhausted.recoverAt = time.Time{}
		}
		return exhausted
	}
	if unknownAfterReset {
		return weeklyCapacity{}
	}
	return result
}

// rank returns an upcoming weekly reset and whether observed capacity blocks
// the model.
func (c *WeeklyQuotaCache) rank(auth *Auth, model string, now time.Time) (time.Time, bool) {
	capacity := c.capacity(auth, model, now)
	return capacity.reset, capacity.exhausted
}

// AttributeWeeklyReservePercent is the per-account reserve override: a top-level
// number in the auth JSON (metadata) or a string attribute, on the 0-100 scale.
const AttributeWeeklyReservePercent = "weekly_reserve_percent"

// NormalizeWeeklyReservePercent clamps a reserve to the 0-100 percent scale of
// WeeklyQuotaBucket.RemainingPercent (100 = the whole week left; not a 0-1
// fraction). Non-finite or non-positive values disable the reserve.
func NormalizeWeeklyReservePercent(value float64) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) || value <= 0 {
		return 0
	}
	return math.Min(value, 100)
}

// authWeeklyReservePercent reads a valid per-account override (0-100).
func authWeeklyReservePercent(auth *Auth) (float64, bool) {
	if auth == nil {
		return 0, false
	}
	raw := auth.Metadata[AttributeWeeklyReservePercent]
	if attribute := strings.TrimSpace(auth.Attributes[AttributeWeeklyReservePercent]); attribute != "" {
		raw = attribute
	}
	var value float64
	switch v := raw.(type) {
	case float64:
		value = v
	case int:
		value = float64(v)
	case json.Number:
		parsed, err := v.Float64()
		if err != nil {
			return 0, false
		}
		value = parsed
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil {
			return 0, false
		}
		value = parsed
	default:
		return 0, false
	}
	if math.IsNaN(value) || value < 0 || value > 100 {
		return 0, false
	}
	return value, true
}

// WeeklyResetFirstSelector uses the earliest upcoming weekly reset within the
// highest available manual-priority tier. Unknown capacity is eligible fallback.
type WeeklyResetFirstSelector struct {
	Cache *WeeklyQuotaCache
	// ReservePercent is the default weekly reserve on the 0-100 scale of
	// WeeklyQuotaBucket.RemainingPercent. 0 disables the reserve.
	ReservePercent float64
	modelForAuth   func(*Auth, string) string
}

func (s *WeeklyResetFirstSelector) selectionModel(auth *Auth, model string) string {
	if s.modelForAuth != nil {
		return s.modelForAuth(auth, model)
	}
	return model
}

// reserveFor returns the account's effective reserve: its own override when
// valid, otherwise the selector default.
func (s *WeeklyResetFirstSelector) reserveFor(auth *Auth) float64 {
	if override, ok := authWeeklyReservePercent(auth); ok {
		return override
	}
	return NormalizeWeeklyReservePercent(s.ReservePercent)
}

// reserved reports whether known current weekly capacity is at or below the
// account's reserve. Unknown, stale, and exhausted capacity is never reserved.
func (s *WeeklyResetFirstSelector) reserved(auth *Auth, capacity weeklyCapacity) bool {
	if !capacity.weekly || capacity.exhausted {
		return false
	}
	reserve := s.reserveFor(auth)
	return reserve > 0 && capacity.remaining <= reserve
}

// filter removes accounts whose observed capacity for the model is exhausted.
// It then removes reserved accounts, but only while another remaining account
// is both unreserved and currently available; otherwise reserved accounts stay
// and normal ranking decides, so the reserve never causes a failed request.
// Unknown capacity is unreserved. Like exhaustion, this runs before priority
// tiers and session-affinity reuse, so a reserved bound thread rebinds.
func (s *WeeklyResetFirstSelector) filter(auths []*Auth, model string, now time.Time) weeklySelection {
	result := weeklySelection{usage: usageExhaustion{model: model}}
	eligible := make([]*Auth, 0, len(auths))
	models := make([]string, 0, len(auths))
	reserved := make([]bool, 0, len(auths))
	reservedCount := 0
	for _, auth := range auths {
		if auth == nil {
			continue
		}
		result.usage.total++
		checkModel := s.selectionModel(auth, model)
		capacity := s.Cache.capacity(auth, checkModel, now)
		if capacity.exhausted {
			result.usage.accounts = append(result.usage.accounts, accountUsageExhaustion{ordinal: result.usage.total, blockers: capacity.blockers, recoverAt: capacity.recoverAt})
			continue
		}
		isReserved := s.reserved(auth, capacity)
		if isReserved {
			reservedCount++
		}
		eligible = append(eligible, auth)
		models = append(models, checkModel)
		reserved = append(reserved, isReserved)
	}
	if reservedCount == 0 || reservedCount == len(eligible) {
		result.auths = eligible
		return result
	}
	openUnreserved := false
	for i, auth := range eligible {
		if reserved[i] {
			continue
		}
		if blocked, _, _ := isAuthBlockedForModel(auth, models[i], now); !blocked {
			openUnreserved = true
			break
		}
	}
	if !openUnreserved {
		result.auths = eligible
		return result
	}
	unreserved := make([]*Auth, 0, len(eligible)-reservedCount)
	for i, auth := range eligible {
		if !reserved[i] {
			unreserved = append(unreserved, auth)
		}
	}
	result.auths = unreserved
	return result
}

func (s *WeeklyResetFirstSelector) eligible(provider, model string, auths []*Auth, now time.Time) (weeklySelection, error) {
	result := s.filter(auths, model, now)
	if len(result.auths) > 0 {
		return result, nil
	}
	var earliest time.Time
	for _, account := range result.usage.accounts {
		if !account.recoverAt.IsZero() && (earliest.IsZero() || account.recoverAt.Before(earliest)) {
			earliest = account.recoverAt
		}
	}
	if earliest.IsZero() {
		return result, nil
	}
	if provider == "mixed" {
		provider = ""
	}
	return result, result.usage.decorate(newUsageExhaustedError(model, provider, earliest, now))
}

func (s *WeeklyResetFirstSelector) Pick(ctx context.Context, provider, model string, opts cliproxyexecutor.Options, auths []*Auth) (*Auth, error) {
	now := time.Now()
	if s.Cache != nil {
		now = s.Cache.nowFunc()
	}
	selection, err := s.eligible(provider, model, auths, now)
	if err != nil {
		return nil, err
	}
	available, err := getSelectorAvailableAuths(ctx, selection.auths, provider, model, now)
	if err != nil {
		return nil, selection.usage.decorate(err)
	}
	available = preferCodexWebsocketAuths(ctx, provider, available)
	ranks := make(map[string]time.Time, len(available))
	for _, auth := range available {
		ranks[auth.ID], _ = s.Cache.rank(auth, s.selectionModel(auth, model), now)
	}
	sort.SliceStable(available, func(i, j int) bool {
		a := ranks[available[i].ID]
		b := ranks[available[j].ID]
		if a.IsZero() != b.IsZero() {
			return !a.IsZero()
		}
		if !a.Equal(b) {
			return a.Before(b)
		}
		return available[i].ID < available[j].ID
	})
	return available[0], nil
}

func weeklySelector(selector Selector) *WeeklyResetFirstSelector {
	switch s := selector.(type) {
	case *WeeklyResetFirstSelector:
		return s
	case *SessionAffinitySelector:
		return weeklySelector(s.fallback)
	default:
		return nil
	}
}

func (m *Manager) bindWeeklySelector(selector Selector) {
	if s := weeklySelector(selector); s != nil {
		s.Cache = m.weeklyQuota
		s.modelForAuth = m.selectionModelForAuth
	}
}

// StartWeeklyQuotaRefresh starts independent usage polling through registered
// executors, retaining their authentication and proxy/transport behavior.
func (m *Manager) StartWeeklyQuotaRefresh(parent context.Context) {
	if parent == nil {
		parent = context.Background()
	}
	m.weeklyLifecycleMu.Lock()
	defer m.weeklyLifecycleMu.Unlock()
	if m.weeklyCancel != nil {
		return
	}
	ctx, cancel := context.WithCancel(parent)
	m.weeklyCancel = cancel
	m.weeklyDone = make(chan struct{})
	go m.weeklyQuotaLoop(ctx, m.weeklyDone)
}

func (m *Manager) StopWeeklyQuotaRefresh() {
	m.weeklyLifecycleMu.Lock()
	defer m.weeklyLifecycleMu.Unlock()
	if m.weeklyCancel == nil {
		return
	}
	m.weeklyCancel()
	<-m.weeklyDone
	m.weeklyCancel = nil
}

func (m *Manager) weeklyQuotaLoop(ctx context.Context, done chan struct{}) {
	defer close(done)
	jobs := make(chan *Auth, 4)
	var workers sync.WaitGroup
	for i := 0; i < 4; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case auth := <-jobs:
					m.refreshWeeklyQuota(ctx, auth)
				}
			}
		}()
	}
	defer workers.Wait()
	ticker := time.NewTicker(weeklyQuotaScanInterval)
	defer ticker.Stop()
	for {
		m.scheduleWeeklyQuota(ctx, jobs)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func weeklyQuotaSupported(auth *Auth) bool {
	return auth != nil && !auth.Disabled && auth.AuthKind() == AuthKindOAuth && (auth.Provider == "claude" || auth.Provider == "codex")
}

func weeklyQuotaDue(o weeklyQuotaObservation, now time.Time) bool {
	if o.LastAttempt.IsZero() || now.Sub(o.LastAttempt) >= weeklyQuotaRefreshInterval {
		return true
	}
	for _, bucket := range o.Buckets {
		if bucket.ResetAt != nil && bucket.ResetAt.After(o.LastAttempt) && !bucket.ResetAt.After(now) {
			return true
		}
	}
	return false
}

func (m *Manager) scheduleWeeklyQuota(ctx context.Context, jobs chan<- *Auth) {
	cache := m.weeklyQuota
	now := cache.nowFunc()
	auths := m.List()
	known := make(map[string]bool, len(auths))
	for _, auth := range auths {
		known[auth.ID] = true
		if !weeklyQuotaSupported(auth) || m.executorFor(auth.Provider) == nil {
			continue
		}
		cache.mu.Lock()
		o := cache.observations[auth.ID]
		if o.Epoch != auth.RegistrationEpoch {
			o = weeklyQuotaObservation{Epoch: auth.RegistrationEpoch}
		}
		_, busy := cache.inFlight[auth.ID]
		if busy || !weeklyQuotaDue(o, now) {
			cache.mu.Unlock()
			continue
		}
		select {
		case <-ctx.Done():
			cache.mu.Unlock()
			return
		case jobs <- auth:
			cache.inFlight[auth.ID] = auth.RegistrationEpoch
			o.LastAttempt = now
			cache.observations[auth.ID] = o
		default:
		}
		cache.mu.Unlock()
	}
	cache.mu.Lock()
	for id := range cache.observations {
		if !known[id] {
			delete(cache.observations, id)
		}
	}
	cache.mu.Unlock()
}

func (m *Manager) refreshWeeklyQuota(ctx context.Context, auth *Auth) {
	defer func() { m.weeklyQuota.mu.Lock(); delete(m.weeklyQuota.inFlight, auth.ID); m.weeklyQuota.mu.Unlock() }()
	if ctx.Err() != nil {
		return
	}
	executor := m.executorFor(auth.Provider)
	if executor == nil {
		return
	}
	endpoint := "https://api.anthropic.com/api/oauth/usage"
	if auth.Provider == "codex" {
		endpoint = "https://chatgpt.com/backend-api/wham/usage"
	}
	if rt := m.roundTripperFor(auth); rt != nil {
		ctx = context.WithValue(ctx, roundTripperContextKey{}, rt)
		ctx = context.WithValue(ctx, "cliproxy.roundtripper", rt)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return
	}
	req.Header.Set("Accept", "application/json")
	if auth.Provider == "claude" {
		req.Header.Set("anthropic-beta", "oauth-2025-04-20")
		req.Header.Set("anthropic-version", "2023-06-01")
	} else {
		if account, ok := auth.Metadata["account_id"].(string); ok && account != "" {
			req.Header.Set("Chatgpt-Account-Id", account)
		}
	}
	resp, err := executor.HttpRequest(ctx, auth, req)
	if resp != nil && resp.Body != nil {
		defer func() { _ = resp.Body.Close() }()
	}
	if err != nil || resp == nil || resp.Body == nil || resp.StatusCode != http.StatusOK {
		return
	}
	const maxQuotaResponse = 1 << 20
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxQuotaResponse+1))
	if err != nil || len(body) > maxQuotaResponse {
		return
	}
	now := m.weeklyQuota.nowFunc()
	buckets, err := parseWeeklyQuota(auth.Provider, body, now)
	if err != nil {
		return
	}
	// Ignore responses from a removed or replaced credential registration.
	m.mu.RLock()
	current := m.auths[auth.ID]
	if current == nil || current.RegistrationEpoch != auth.RegistrationEpoch || current.Disabled {
		m.mu.RUnlock()
		return
	}
	m.weeklyQuota.mu.Lock()
	o := m.weeklyQuota.observations[auth.ID]
	o.Epoch, o.ObservedAt, o.Buckets = auth.RegistrationEpoch, now, buckets
	m.weeklyQuota.observations[auth.ID] = o
	m.weeklyQuota.mu.Unlock()
	m.mu.RUnlock()
}

func parseWeeklyQuota(provider string, body []byte, observedAt time.Time) ([]WeeklyQuotaBucket, error) {
	if !gjson.ValidBytes(body) {
		return nil, fmt.Errorf("invalid usage response")
	}
	root := gjson.ParseBytes(body)
	first := func(value gjson.Result, fields ...string) gjson.Result {
		for _, field := range fields {
			if found := value.Get(field); found.Exists() && found.Type != gjson.Null {
				return found
			}
		}
		return gjson.Result{}
	}
	percent := func(value gjson.Result) *float64 {
		if value.Type != gjson.Number {
			return nil
		}
		v := value.Float()
		return &v
	}
	parseDate := func(value gjson.Result) *time.Time {
		if value.Type != gjson.String {
			return nil
		}
		parsed, err := time.Parse(time.RFC3339, value.String())
		if err != nil {
			return nil
		}
		return &parsed
	}
	var buckets []WeeklyQuotaBucket
	appendBucket := func(scope, window string, used *float64, reset *time.Time) {
		if used == nil || math.IsNaN(*used) || math.IsInf(*used, 0) || *used < 0 || *used > 100 {
			return
		}
		buckets = append(buckets, WeeklyQuotaBucket{Scope: scope, Window: window, ResetAt: reset, RemainingPercent: 100 - *used})
	}
	if provider == "claude" {
		for _, field := range []struct{ key, scope, window string }{{"seven_day", "all", "weekly"}, {"seven_day_sonnet", "sonnet", "weekly"}, {"seven_day_opus", "opus", "weekly"}, {"five_hour", "all", "five-hour"}} {
			value := root.Get(field.key)
			appendBucket(field.scope, field.window, percent(value.Get("utilization")), parseDate(value.Get("resets_at")))
		}
		root.Get("limits").ForEach(func(_, value gjson.Result) bool {
			if value.Get("kind").String() != "weekly_scoped" || (value.Get("is_active").Type == gjson.False) {
				return true
			}
			scope := strings.ToLower(strings.TrimSpace(value.Get("scope.model.id").String()))
			if scope == "" {
				display := strings.ToLower(value.Get("scope.model.display_name").String())
				for _, family := range []string{"sonnet", "opus", "fable"} {
					if strings.Contains(display, family) {
						scope = family
						break
					}
				}
			}
			if scope != "" {
				appendBucket(scope, "weekly", percent(value.Get("percent")), parseDate(value.Get("resets_at")))
			}
			return true
		})
	} else if provider == "codex" {
		parseLimit := func(scope string, limit gjson.Result) {
			if !limit.IsObject() {
				return
			}
			blocked := first(limit, "allowed").Type == gjson.False || first(limit, "limit_reached", "limitReached").Type == gjson.True
			before := len(buckets)
			for _, window := range []gjson.Result{first(limit, "primary_window", "primaryWindow", "primary"), first(limit, "secondary_window", "secondaryWindow", "secondary")} {
				seconds := first(window, "limit_window_seconds", "limitWindowSeconds").Int()
				if seconds == 0 {
					minutes := first(window, "window_minutes", "windowMinutes").Int()
					if minutes == 300 || minutes == 10080 {
						seconds = minutes * 60
					}
				}
				kind := ""
				switch seconds {
				case 604800:
					kind = "weekly"
				case 18000:
					kind = "five-hour"
				default:
					continue
				}
				var reset *time.Time
				resetAt := first(window, "reset_at", "resetAt").Int()
				resetAfter := first(window, "reset_after_seconds", "resetAfterSeconds")
				if resetAt > 0 && resetAt <= 253402300799 {
					value := time.Unix(resetAt, 0)
					reset = &value
				} else if resetAfter.Type == gjson.Number && resetAfter.Int() >= 0 && resetAfter.Int() <= 366*24*60*60 {
					value := observedAt.Add(time.Duration(resetAfter.Int()) * time.Second)
					reset = &value
				}
				used := percent(first(window, "used_percent", "usedPercent"))
				appendBucket(scope, kind, used, reset)
			}
			// Explicit refusal with missing percentages still blocks capacity.
			// Label it capacity, never fabricate a weekly reset from a monthly limit.
			if blocked {
				used := 100.0
				var reset *time.Time
				for _, bucket := range buckets[before:] {
					if bucket.ResetAt != nil && (reset == nil || bucket.ResetAt.Before(*reset)) {
						reset = bucket.ResetAt
					}
				}
				appendBucket(scope, "capacity", &used, reset)
			}
		}
		parseLimit("all", first(root, "rate_limit", "rateLimit", "rate_limits"))
		additional := first(root, "additional_rate_limits", "additionalRateLimits")
		additional.ForEach(func(key, value gjson.Result) bool {
			name := strings.ToLower(strings.TrimSpace(first(value, "limit_name", "limitName", "name").String()))
			if name == "" && additional.IsObject() {
				name = strings.ToLower(strings.TrimSpace(key.String()))
			}
			if name != "" && len(name) <= 128 && strings.IndexFunc(name, func(r rune) bool { return r < 32 || r == 127 }) == -1 {
				limit := first(value, "rate_limit", "rateLimit")
				if !limit.Exists() {
					limit = value
				}
				parseLimit(name, limit)
			}
			return true
		})
	}
	if len(buckets) == 0 {
		return nil, fmt.Errorf("usage response has no recognized capacity windows")
	}
	return buckets, nil
}

// WeeklyQuotaAccountStatus is a safe management view with no filenames, emails,
// raw auth IDs, access tokens, or provider response bodies.
type WeeklyQuotaAccountStatus struct {
	AuthIndex        string     `json:"auth_index"`
	Provider         string     `json:"provider"`
	Status           string     `json:"status"`
	WeeklyResetAt    *time.Time `json:"weekly_reset_at,omitempty"`
	RemainingPercent *float64   `json:"remaining_percent,omitempty"`
	// ReservePercent is the account's effective weekly reserve (0-100).
	ReservePercent float64 `json:"reserve_percent"`
	// Reserved reports account-wide weekly capacity at or below the reserve. Such
	// accounts are skipped while another available account is unreserved.
	// Model-scoped weekly buckets can also reserve an account for those models.
	Reserved   bool                `json:"reserved"`
	ObservedAt *time.Time          `json:"observed_at,omitempty"`
	Buckets    []WeeklyQuotaBucket `json:"buckets"`
}

var weeklyQuotaPacific, _ = time.LoadLocation("America/Los_Angeles")

func weeklyPacificTime(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	if weeklyQuotaPacific != nil {
		t = t.In(weeklyQuotaPacific)
	}
	return &t
}

// WeeklyReservePercent reports the active weekly-reset-first default reserve
// (0-100), or 0 when another strategy is active.
func (m *Manager) WeeklyReservePercent() float64 {
	if m == nil {
		return 0
	}
	if s := weeklySelector(m.Selector()); s != nil {
		return NormalizeWeeklyReservePercent(s.ReservePercent)
	}
	return 0
}

func (m *Manager) WeeklyQuotaStatus() []WeeklyQuotaAccountStatus {
	weekly := weeklySelector(m.Selector())
	auths := m.List()
	result := make([]WeeklyQuotaAccountStatus, 0)
	now := m.weeklyQuota.nowFunc()
	for _, auth := range auths {
		if auth.Provider != "claude" && auth.Provider != "codex" {
			continue
		}
		row := WeeklyQuotaAccountStatus{AuthIndex: auth.EnsureIndex(), Provider: auth.Provider, Status: "unknown", Buckets: make([]WeeklyQuotaBucket, 0)}
		if weekly != nil {
			row.ReservePercent = weekly.reserveFor(auth)
		}
		if observation, ok := m.weeklyQuota.observation(auth); ok {
			row.ObservedAt = weeklyPacificTime(observation.ObservedAt)
			row.Buckets = make([]WeeklyQuotaBucket, len(observation.Buckets))
			for i, bucket := range observation.Buckets {
				row.Buckets[i] = bucket
				if bucket.ResetAt != nil {
					row.Buckets[i].ResetAt = weeklyPacificTime(*bucket.ResetAt)
				}
				if bucket.Scope == "all" && bucket.Window == "weekly" {
					remaining := bucket.RemainingPercent
					row.RemainingPercent = &remaining
					row.WeeklyResetAt = row.Buckets[i].ResetAt
				}
			}
			capacity := m.weeklyQuota.capacity(auth, "", now)
			if now.Sub(observation.ObservedAt) >= weeklyQuotaStaleTTL {
				row.Status = "stale"
			} else if capacity.exhausted {
				row.Status = "exhausted"
			} else if !capacity.reset.IsZero() {
				row.Status = "ready"
			}
			row.Reserved = weekly != nil && weekly.reserved(auth, capacity)
		}
		if auth.Disabled {
			row.Status = "disabled"
		} else if auth.Unavailable {
			row.Status = "unavailable"
		}
		result = append(result, row)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].AuthIndex < result[j].AuthIndex })
	return result
}
