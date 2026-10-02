package executor

import (
	"net/http"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
)

func TestClaudeUsageRequestUsesConfiguredSoftwareBaseline(t *testing.T) {
	for _, test := range []struct{ name, configured, caller, want string }{
		{"default", "", "", "claude-cli/2.1.280 (external, cli)"},
		{"configured", "custom-cli/1.0", "", "custom-cli/1.0"},
		{"explicit request", "custom-cli/1.0", "request-agent/2.0", "request-agent/2.0"},
	} {
		t.Run(test.name, func(t *testing.T) {
			exec := NewClaudeExecutor(&config.Config{ClaudeHeaderDefaults: config.ClaudeHeaderDefaults{UserAgent: test.configured}})
			auth := &cliproxyauth.Auth{Provider: "claude", Metadata: map[string]any{"access_token": "test-token"}}
			req, err := http.NewRequest(http.MethodGet, "https://api.anthropic.com/api/oauth/usage", nil)
			if err != nil {
				t.Fatal(err)
			}
			if test.caller != "" {
				req.Header.Set("User-Agent", test.caller)
			}
			if err = exec.PrepareRequest(req, auth); err != nil {
				t.Fatal(err)
			}
			if got := req.Header.Get("User-Agent"); got != test.want {
				t.Fatalf("User-Agent = %q, want %q", got, test.want)
			}
			if got := req.Header.Get("Authorization"); got != "Bearer test-token" {
				t.Fatal("usage auth injection changed")
			}
			if len(auth.Metadata) != 1 {
				t.Fatal("usage header preparation mutated auth metadata")
			}
		})
	}
}
