package executor

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
)

func TestCodexAutoDefaultWebsocketsForHTTPAndSSEReuseSession(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%v", stream), func(t *testing.T) {
			var upgrades, messages atomic.Int32
			upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
					t.Error("default OAuth request used HTTP upstream")
					w.WriteHeader(500)
					return
				}
				if r.Header.Get("Authorization") != "Bearer access-test" || r.Header.Get("Chatgpt-Account-Id") != "account-test" {
					t.Error("credential injection was lost")
				}
				conn, err := upgrader.Upgrade(w, r, nil)
				if err != nil {
					t.Errorf("upgrade: %v", err)
					return
				}
				upgrades.Add(1)
				defer func() { _ = conn.Close() }()
				for {
					if _, _, err := conn.ReadMessage(); err != nil {
						return
					}
					messages.Add(1)
					completed := []byte(`{"type":"response.completed","response":{"id":"resp-test","status":"completed","output":[],"usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0}}}`)
					if err := conn.WriteMessage(websocket.TextMessage, completed); err != nil {
						t.Errorf("write: %v", err)
						return
					}
				}
			}))
			defer server.Close()
			exec := NewCodexAutoExecutor(&config.Config{SDKConfig: config.SDKConfig{DisableImageGeneration: config.DisableImageGenerationAll}})
			session := fmt.Sprintf("default-ws-test-%v", stream)
			defer exec.CloseExecutionSession(session)
			auth := &cliproxyauth.Auth{ID: "oauth-default", Provider: "codex", Attributes: map[string]string{"base_url": server.URL}, Metadata: map[string]any{"access_token": "access-test", "account_id": "account-test"}}
			opts := cliproxyexecutor.Options{SourceFormat: sdktranslator.FromString("codex"), ResponseFormat: sdktranslator.FromString("codex"), Metadata: map[string]any{cliproxyexecutor.ExecutionSessionMetadataKey: session}}
			for i := 0; i < 2; i++ {
				req := cliproxyexecutor.Request{Model: "gpt-5-codex", Payload: []byte(`{"model":"gpt-5-codex","input":[],"prompt_cache_key":"thread-test"}`)}
				if stream {
					result, err := exec.ExecuteStream(context.Background(), auth, req, opts)
					if err != nil {
						t.Fatal(err)
					}
					for chunk := range result.Chunks {
						if chunk.Err != nil {
							t.Fatal(chunk.Err)
						}
					}
				} else if _, err := exec.Execute(context.Background(), auth, req, opts); err != nil {
					t.Fatal(err)
				}
			}
			if upgrades.Load() != 1 || messages.Load() != 2 {
				t.Fatalf("upgrades/messages = %d/%d, want 1/2", upgrades.Load(), messages.Load())
			}
		})
	}
}

func TestCodexAutoWebsocketExplicitOptOutAndUpgradeFallback(t *testing.T) {
	for _, mode := range []string{"credential-false", "provider-false", "upgrade-fallback"} {
		t.Run(mode, func(t *testing.T) {
			var ws, httpCalls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
					ws.Add(1)
					w.WriteHeader(http.StatusUpgradeRequired)
					return
				}
				httpCalls.Add(1)
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = fmt.Fprint(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp-test\",\"status\":\"completed\",\"output\":[],\"usage\":{\"input_tokens\":0,\"output_tokens\":0,\"total_tokens\":0}}}\n\n")
			}))
			defer server.Close()
			cfg := &config.Config{SDKConfig: config.SDKConfig{DisableImageGeneration: config.DisableImageGenerationAll}}
			auth := &cliproxyauth.Auth{ID: "test", Provider: "codex", Attributes: map[string]string{"base_url": server.URL}, Metadata: map[string]any{"access_token": "test"}}
			if mode == "credential-false" {
				auth.Metadata["websockets"] = false
			}
			if mode == "provider-false" {
				disabled := false
				cfg.Codex.UpstreamWebsockets = &disabled
				auth.Metadata["websockets"] = true
			}
			exec := NewCodexAutoExecutor(cfg)
			result, err := exec.ExecuteStream(context.Background(), auth, cliproxyexecutor.Request{Model: "gpt-5-codex", Payload: []byte(`{"model":"gpt-5-codex","input":[]}`)}, cliproxyexecutor.Options{SourceFormat: sdktranslator.FromString("codex")})
			if err != nil {
				t.Fatal(err)
			}
			for chunk := range result.Chunks {
				if chunk.Err != nil {
					t.Fatal(chunk.Err)
				}
			}
			wantWS := int32(0)
			if mode == "upgrade-fallback" {
				wantWS = 1
			}
			if ws.Load() != wantWS || httpCalls.Load() != 1 {
				t.Fatalf("WS/HTTP = %d/%d, want %d/1", ws.Load(), httpCalls.Load(), wantWS)
			}
		})
	}
}
