package cliproxy

import (
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
