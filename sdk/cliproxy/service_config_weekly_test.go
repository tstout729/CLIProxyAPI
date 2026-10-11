package cliproxy

import (
	"context"
	"testing"

	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/config"
)

func TestRoutingRuntimeWeeklyStrategy(t *testing.T) {
	cfg := &config.Config{}
	cfg.Routing.Strategy = "weekly-reset-first"
	state := normalizedRoutingRuntimeState(cfg)
	if state.strategy != "weekly-reset-first" {
		t.Fatalf("strategy = %q", state.strategy)
	}
	if _, ok := newRoutingSelector(state).(*coreauth.WeeklyResetFirstSelector); !ok {
		t.Fatal("weekly selector was not wired")
	}
	cfg.Routing.SessionAffinity = true
	if _, ok := newRoutingSelector(normalizedRoutingRuntimeState(cfg)).(*coreauth.SessionAffinitySelector); !ok {
		t.Fatal("weekly selector was not wrapped in affinity")
	}
}

func TestRoutingRuntimeWeeklyReservePercent(t *testing.T) {
	cfg := &config.Config{}
	cfg.Routing.Strategy = "weekly-reset-first"
	if state := normalizedRoutingRuntimeState(cfg); state.weeklyReservePercent != 0 {
		t.Fatalf("default reserve = %v", state.weeklyReservePercent)
	}
	for raw, want := range map[float64]float64{15: 15, 12.5: 12.5, -3: 0, 150: 100} {
		cfg.Routing.WeeklyReservePercent = raw
		state := normalizedRoutingRuntimeState(cfg)
		if state.weeklyReservePercent != want {
			t.Fatalf("reserve %v normalized to %v, want %v", raw, state.weeklyReservePercent, want)
		}
		selector, ok := newRoutingSelector(state).(*coreauth.WeeklyResetFirstSelector)
		if !ok || selector.ReservePercent != want {
			t.Fatalf("selector = %#v, want reserve %v", selector, want)
		}
	}
	// Other strategies ignore the reserve, so changing it cannot rebuild their selector.
	cfg.Routing.Strategy = "round-robin"
	if state := normalizedRoutingRuntimeState(cfg); state.weeklyReservePercent != 0 {
		t.Fatalf("round-robin carried reserve %v", state.weeklyReservePercent)
	}
}

func TestApplyManagerConfigHotReloadsWeeklyReserve(t *testing.T) {
	service := &Service{coreManager: coreauth.NewManager(nil, nil, nil)}
	apply := func(sequence uint64, reserve float64, affinity bool) {
		t.Helper()
		cfg := &config.Config{}
		cfg.Routing.Strategy = "weekly-reset-first"
		cfg.Routing.SessionAffinity = affinity
		cfg.Routing.WeeklyReservePercent = reserve
		if !service.applyManagerConfig(context.Background(), configCommit{cfg: cfg, sequence: sequence}) {
			t.Fatal("applyManagerConfig failed")
		}
		if got := service.coreManager.WeeklyReservePercent(); got != reserve {
			t.Fatalf("active reserve = %v, want %v", got, reserve)
		}
	}
	apply(1, 0, true)
	apply(2, 15, true)
	if selector, ok := service.coreManager.Selector().(*coreauth.SessionAffinitySelector); !ok || selector == nil {
		t.Fatalf("selector = %T, want session affinity", service.coreManager.Selector())
	}
	apply(3, 25, false)
	apply(4, 0, false)
}
