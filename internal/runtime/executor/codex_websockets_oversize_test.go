package executor

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/usage"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestCodexAutoOversizedRequestTransport(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, mode := range []string{"small", "below-limit", "oversized", "lifecycle", "downstream-websocket", "required-websocket", "previous-response", "prewarm"} {
			t.Run(fmt.Sprintf("stream=%v/%s", stream, mode), func(t *testing.T) {
				var wsCalls, httpCalls atomic.Int32
				upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
				const completed = `{"type":"response.completed","response":{"id":"resp-transport","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":0,"total_tokens":1}}}`
				inputSize := 16 * 1024 * 1024
				if mode == "small" {
					inputSize = 128
				} else if mode == "below-limit" {
					inputSize -= 1024
				}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Header.Get("Authorization") != "Bearer access-test" {
						t.Error("upstream credentials were lost")
					}
					var body []byte
					var conn *websocket.Conn
					var err error
					if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
						wsCalls.Add(1)
						conn, err = upgrader.Upgrade(w, r, nil)
						if err != nil {
							t.Errorf("upgrade: %v", err)
							return
						}
						defer func() { _ = conn.Close() }()
						_, body, err = conn.ReadMessage()
					} else {
						httpCalls.Add(1)
						body, err = io.ReadAll(r.Body)
					}
					if err != nil {
						t.Errorf("read request: %v", err)
						return
					}
					if got := len(gjson.GetBytes(body, "input.0.content").String()); got != inputSize {
						t.Errorf("input bytes = %d, want %d", got, inputSize)
					}
					if conn != nil {
						if mode == "previous-response" && gjson.GetBytes(body, "previous_response_id").String() != "resp-prior" {
							t.Error("previous response context was lost")
						}
						if mode == "prewarm" && gjson.GetBytes(body, "generate").Type != gjson.False {
							t.Error("prewarm became generation")
						}
						if err := conn.WriteMessage(websocket.TextMessage, []byte(completed)); err != nil {
							t.Errorf("write completion: %v", err)
						}
					} else {
						w.Header().Set("Content-Type", "text/event-stream")
						_, _ = fmt.Fprintf(w, "data: %s\n\n", completed)
					}
				}))
				defer server.Close()

				exec := NewCodexAutoExecutor(&config.Config{SDKConfig: config.SDKConfig{DisableImageGeneration: config.DisableImageGenerationAll}})
				exec.wsExec.store = &codexWebsocketSessionStore{sessions: make(map[string]*codexWebsocketSession)}
				defer exec.CloseExecutionSession(t.Name())
				auth := &cliproxyauth.Auth{ID: t.Name(), Provider: "codex", Attributes: map[string]string{"base_url": server.URL}, Metadata: map[string]any{"access_token": "access-test"}}
				opts := cliproxyexecutor.Options{
					SourceFormat:   sdktranslator.FromString("codex"),
					ResponseFormat: sdktranslator.FromString("codex"),
					Metadata:       map[string]any{cliproxyexecutor.ExecutionSessionMetadataKey: t.Name()},
				}
				ctx := context.Background()
				extra := ""
				switch mode {
				case "lifecycle":
					opts.ExecutionLifecycle = newTerminalFailureLifecycle()
				case "downstream-websocket":
					ctx = cliproxyexecutor.WithDownstreamWebsocket(ctx)
				case "required-websocket":
					ctx = cliproxyexecutor.WithRequiredUpstreamWebsocket(ctx)
				case "previous-response":
					extra = `,"previous_response_id":"resp-prior"`
				case "prewarm":
					extra = `,"generate":false`
				}
				req := cliproxyexecutor.Request{Model: "gpt-5-codex", Payload: []byte(fmt.Sprintf(`{"model":"gpt-5-codex","input":[{"role":"user","content":"%s"}]%s}`, strings.Repeat("x", inputSize), extra))}
				var err error
				var output []byte
				if stream {
					var result *cliproxyexecutor.StreamResult
					result, err = exec.ExecuteStream(ctx, auth, req, opts)
					if err == nil {
						for chunk := range result.Chunks {
							if chunk.Err != nil {
								err = chunk.Err
							}
							output = append(output, chunk.Payload...)
						}
					}
				} else {
					var response cliproxyexecutor.Response
					response, err = exec.Execute(ctx, auth, req, opts)
					output = response.Payload
				}
				if mode == "required-websocket" {
					if err == nil || wsCalls.Load() != 0 || httpCalls.Load() != 0 {
						t.Fatalf("required websocket: err=%v WS/HTTP=%d/%d, want local replay error without upstream calls", err, wsCalls.Load(), httpCalls.Load())
					}
					return
				}
				if err != nil {
					t.Fatal(err)
				}
				if !strings.Contains(string(output), "resp-transport") {
					t.Fatalf("completion was lost: %s", output)
				}
				wantWS, wantHTTP := int32(1), int32(0)
				if mode == "oversized" {
					wantWS, wantHTTP = 0, 1
					exec.wsExec.store.mu.Lock()
					sessions := len(exec.wsExec.store.sessions)
					exec.wsExec.store.mu.Unlock()
					if sessions != 0 {
						t.Errorf("HTTP fallback allocated %d websocket sessions", sessions)
					}
				}
				if wsCalls.Load() != wantWS || httpCalls.Load() != wantHTTP {
					t.Fatalf("WS/HTTP = %d/%d, want %d/%d", wsCalls.Load(), httpCalls.Load(), wantWS, wantHTTP)
				}
			})
		}
	}
}

func TestCodexWebsocketHTTPFallbackReportsFailureOnce(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, mode := range []string{"oversized", "upgrade-required"} {
			t.Run(fmt.Sprintf("stream=%v/%s", stream, mode), func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method == http.MethodGet {
						w.WriteHeader(http.StatusUpgradeRequired)
						return
					}
					_, _ = io.Copy(io.Discard, r.Body)
					http.Error(w, `{"error":{"message":"test rejection"}}`, http.StatusBadRequest)
				}))
				defer server.Close()
				capture := &task6UsageCapture{id: t.Name(), records: make(chan usage.Record, 8)}
				usage.RegisterNamedPlugin(t.Name(), capture)
				defer usage.RegisterNamedPlugin(t.Name(), codexResponseModelNoopUsagePlugin{})
				exec := NewCodexAutoExecutor(&config.Config{SDKConfig: config.SDKConfig{DisableImageGeneration: config.DisableImageGenerationAll}})
				auth := &cliproxyauth.Auth{ID: t.Name(), Provider: "codex", Attributes: map[string]string{"base_url": server.URL}, Metadata: map[string]any{"access_token": "access-test"}}
				inputSize := 128
				if mode == "oversized" {
					inputSize = 16 * 1024 * 1024
				}
				req := cliproxyexecutor.Request{Model: "gpt-5-codex", Payload: []byte(fmt.Sprintf(`{"model":"gpt-5-codex","input":[{"role":"user","content":"%s"}]}`, strings.Repeat("x", inputSize)))}
				opts := cliproxyexecutor.Options{SourceFormat: sdktranslator.FromString("codex")}
				var err error
				if stream {
					_, err = exec.ExecuteStream(context.Background(), auth, req, opts)
				} else {
					_, err = exec.Execute(context.Background(), auth, req, opts)
				}
				if err == nil {
					t.Fatal("expected HTTP rejection")
				}
				usage.PublishRecord(context.Background(), usage.Record{RequestID: t.Name() + "-barrier"})
				count := 0
				for {
					select {
					case record := <-capture.records:
						if record.RequestID == t.Name()+"-barrier" {
							if count != 1 {
								t.Errorf("failure usage records = %d, want 1", count)
							}
							return
						}
						count++
						if !record.Failed {
							t.Error("HTTP rejection recorded as success")
						}
					case <-time.After(3 * time.Second):
						t.Fatal("usage barrier did not arrive")
					}
				}
			})
		}
	}
}
