package config

import "testing"

func TestRoutingWeeklyReservePercentParsing(t *testing.T) {
	for _, tc := range []struct {
		name string
		raw  string
		v8   bool
		want float64
	}{
		{name: "default", raw: "routing:\n  strategy: weekly-reset-first\n", want: 0},
		{name: "legacy layout", raw: "routing:\n  strategy: weekly-reset-first\n  weekly-reserve-percent: 15\n", want: 15},
		{name: "fractional percent", raw: "routing:\n  strategy: weekly-reset-first\n  weekly-reserve-percent: 12.5\n", want: 12.5},
		{name: "v8 layout", v8: true, raw: "config-version: 8\nserver:\n  port: 8318\nrouting:\n  strategy: \"weekly-reset-first\"\n  weekly-reserve-percent: 15\n  session-affinity: true\n", want: 15},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.v8 {
				if err := ValidateV8Config([]byte(tc.raw)); err != nil {
					t.Fatalf("ValidateV8Config: %v", err)
				}
			}
			cfg, err := ParseConfigBytes([]byte(tc.raw))
			if err != nil {
				t.Fatal(err)
			}
			if cfg.Routing.WeeklyReservePercent != tc.want || cfg.Routing.Strategy != "weekly-reset-first" {
				t.Fatalf("routing = %+v, want weekly-reserve-percent %v", cfg.Routing, tc.want)
			}
		})
	}
}
