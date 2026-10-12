package openai

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	runtimeexecutor "github.com/router-for-me/CLIProxyAPI/v8/internal/runtime/executor"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/api/handlers"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	"github.com/tidwall/gjson"
)

// Exercise the actual handler, auth manager and Codex auto executor, including
// the default configuration where steering is disabled. Protocol events order
// every action; deadlines only bound failures and never drive a successful test.
func TestResponsesInterruptFullDuplexIntegration(t *testing.T) {
	for _, steering := range []bool{false, true} {
		for _, buffering := range []bool{false, true} {
			for _, partial := range []bool{false, true} {
				name := fmt.Sprintf("steering_%t/buffering_%t/partial_%t", steering, buffering, partial)
				t.Run(name, func(t *testing.T) {
					testResponsesInterruptIntegration(t, steering, buffering, partial)
				})
			}
		}
	}
}

func testResponsesInterruptIntegration(t *testing.T, steering, buffering, partial bool) {
	t.Helper()
	control := []byte(`{ "type":"response.interrupt", "response_id":"r1", "mode":"discard_partial_items", "future_control_hint":{"preserve":true} }`)
	interrupted := []byte(`{"type":"response.incomplete","response":{"id":"r1","status":"incomplete","incomplete_details":{"reason":"interrupted"},"output":[],"usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0}}}`)
	var connections atomic.Int32
	done := make(chan struct{})
	staleSent := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		connection := connections.Add(1)
		if connection == 1 {
			defer close(done)
		}
		c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upstream upgrade: %v", err)
			return
		}
		defer func() { _ = c.Close() }()
		if connection != 1 {
			t.Errorf("unexpected upstream reconnect: connection %d", connection)
			return
		}
		_ = c.SetReadDeadline(time.Now().Add(4 * time.Second))
		read := func() ([]byte, bool) {
			kind, payload, errRead := c.ReadMessage()
			if errRead != nil {
				t.Errorf("upstream read: %v", errRead)
				return nil, false
			}
			if kind != websocket.TextMessage {
				t.Errorf("upstream received message type %d", kind)
				return nil, false
			}
			return payload, true
		}
		write := func(payload []byte) bool {
			if errWrite := c.WriteMessage(websocket.TextMessage, payload); errWrite != nil {
				t.Errorf("upstream write: %v", errWrite)
				return false
			}
			return true
		}
		initial, ok := read()
		if !ok {
			return
		}
		if gjson.GetBytes(initial, "type").String() != "response.create" {
			t.Errorf("expected initial create, got %s", initial)
			return
		}
		if !write([]byte(`{"type":"response.created","response":{"id":"r1","status":"in_progress","output":[]}}`)) {
			return
		}
		if !partial && !write([]byte(`{"type":"codex.response.metadata","metadata":{"conversation_id":"interrupt-conversation"}}`)) {
			return
		}
		// An empty delta releases bootstrap buffering without generated content
		// or tokens. The client still observes created before sending the control.
		if !partial && !write([]byte(`{"type":"response.output_text.delta","response_id":"r1","item_id":"empty-item","output_index":0,"content_index":0,"delta":""}`)) {
			return
		}
		if partial {
			if !write([]byte(`{"type":"response.output_text.delta","response_id":"r1","item_id":"partial-item","output_index":0,"content_index":0,"delta":"PARTIAL"}`)) ||
				!write([]byte(`{"type":"response.output_item.done","response_id":"r1","output_index":0,"item":{"id":"partial-item","type":"message","role":"assistant","content":[{"type":"output_text","text":"PARTIAL"}]}}`)) {
				return
			}
		}
		// The server cannot finish until the active interrupt arrives, so the test
		// also catches handlers that only read controls after generation completes.
		actualControl, ok := read()
		if !ok {
			return
		}
		if !bytes.Equal(actualControl, control) {
			t.Errorf("interrupt bytes changed: got %s; want %s", actualControl, control)
			return
		}
		if !write(interrupted) {
			return
		}
		next, ok := read()
		if !ok {
			return
		}
		if gjson.GetBytes(next, "type").String() != "response.create" ||
			gjson.GetBytes(next, "instructions").String() != "AFTER_INTERRUPT" ||
			gjson.GetBytes(next, "previous_response_id").Exists() ||
			len(gjson.GetBytes(next, "input").Array()) != 1 ||
			!strings.Contains(gjson.GetBytes(next, "input").Raw, "AFTER_INTERRUPT") ||
			bytes.Contains(next, []byte("PARTIAL")) {
			t.Errorf("next create was not self-contained: %s", next)
			return
		}
		if !write([]byte(`{"type":"response.created","response":{"id":"r2","status":"in_progress","output":[]}}`)) {
			return
		}
		// A substantive event releases the configured bootstrap window for r2.
		if !write([]byte(`{"type":"response.output_text.delta","response_id":"r2","item_id":"next-item","output_index":0,"content_index":0,"delta":"NEXT_OK"}`)) {
			return
		}
		select {
		case <-staleSent:
		case <-time.After(3 * time.Second):
			t.Error("downstream did not send the stale interrupt after r2 created")
			return
		}
		if !write([]byte(`{"type":"response.output_item.done","response_id":"r2","output_index":0,"item":{"id":"next-item","type":"message","role":"assistant","content":[{"type":"output_text","text":"NEXT_OK"}]}}`)) ||
			!write([]byte(`{"type":"response.completed","response":{"id":"r2","status":"completed","output":[]}}`)) {
			return
		}
		third, ok := read()
		if !ok {
			return
		}
		if gjson.GetBytes(third, "type").String() != "response.create" || gjson.GetBytes(third, "instructions").String() != "AFTER_STALE_INTERRUPT" {
			t.Errorf("stale interrupt reached upstream or blocked the next create: %s", third)
			return
		}
		if !write([]byte(`{"type":"response.created","response":{"id":"r3","status":"in_progress","output":[]}}`)) ||
			!write([]byte(`{"type":"response.completed","response":{"id":"r3","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"STALE_OK"}]}]}}`)) {
			return
		}
		// Stay connected until the downstream closes. Any unexpected replay or
		// extra create is observable on this same upstream connection.
		if _, extra, errRead := c.ReadMessage(); errRead == nil {
			t.Errorf("unexpected frame after next completion: %s", extra)
		}
	}))
	defer upstream.Close()
	cfg := &config.Config{}
	if steering {
		cfg.Codex.ResponseSteering = true
		cfg.CodexResponseSteering = true
	}
	cfg.Codex.StreamBootstrapBuffering = buffering
	manager := coreauth.NewManager(nil, nil, nil)
	manager.SetConfig(cfg)
	manager.RegisterExecutor(runtimeexecutor.NewCodexAutoExecutor(cfg))
	authID := fmt.Sprintf("interrupt-integration-%t-%t-%t", steering, buffering, partial)
	model := "interrupt-integration-model"
	if _, err := manager.Register(context.Background(), &coreauth.Auth{
		ID: authID, Provider: "codex", Status: coreauth.StatusActive,
		Attributes: map[string]string{"api_key": "test-key", "base_url": upstream.URL, "websockets": "true"},
	}); err != nil {
		t.Fatal(err)
	}
	registry.GetGlobalRegistry().RegisterClient(authID, "codex", []*registry.ModelInfo{{ID: model}})
	defer registry.GetGlobalRegistry().UnregisterClient(authID)
	h := NewOpenAIResponsesAPIHandler(handlers.NewBaseAPIHandlers(&cfg.SDKConfig, manager))
	router := gin.New()
	router.GET("/v1/responses", h.ResponsesWebsocket)
	downstream := httptest.NewServer(router)
	defer downstream.Close()
	c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(downstream.URL, "http")+"/v1/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = c.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("upstream connection cleanup stalled")
		}
	}()
	_ = c.SetReadDeadline(time.Now().Add(5 * time.Second))
	send := func(payload []byte) {
		if errWrite := c.WriteMessage(websocket.TextMessage, payload); errWrite != nil {
			t.Fatalf("downstream write: %v", errWrite)
		}
	}
	send([]byte(fmt.Sprintf(`{"type":"response.create","model":%q,"input":[]}`, model)))
	createdSeen, deltaSeen, partialItemSeen, controlSent, interruptedSeen, nextCreatedSeen := false, false, false, false, false, false
	nextCompletedSeen := false
	for {
		_, payload, errRead := c.ReadMessage()
		if errRead != nil {
			t.Fatalf("socket closed before next response completed: %v", errRead)
		}
		switch gjson.GetBytes(payload, "type").String() {
		case "response.created":
			if gjson.GetBytes(payload, "response.id").String() == "r1" {
				createdSeen = true
				if !partial && !controlSent {
					send(control)
					controlSent = true
				}
			} else if gjson.GetBytes(payload, "response.id").String() == "r2" {
				nextCreatedSeen = true
				send(control)
				close(staleSent)
			}
		case "response.output_text.delta":
			if gjson.GetBytes(payload, "response_id").String() == "r1" {
				deltaSeen = gjson.GetBytes(payload, "delta").String() == "PARTIAL"
			}
		case "response.output_item.done":
			if gjson.GetBytes(payload, "response_id").String() == "r1" {
				partialItemSeen = true
				send(control)
				controlSent = true
			}
		case "response.incomplete":
			if !createdSeen || !controlSent || interruptedSeen || gjson.GetBytes(payload, "response.id").String() != "r1" {
				t.Fatalf("unexpected interrupted event ordering: %s", payload)
			}
			if partial && (!deltaSeen || !partialItemSeen) {
				t.Fatalf("interrupt terminal preceded partial output: %s", payload)
			}
			if gjson.GetBytes(payload, "response.incomplete_details.reason").String() != "interrupted" ||
				gjson.GetBytes(payload, "response.output").Raw != "[]" ||
				!gjson.GetBytes(payload, "response.usage.input_tokens").Exists() ||
				!gjson.GetBytes(payload, "response.usage.output_tokens").Exists() ||
				!gjson.GetBytes(payload, "response.usage.total_tokens").Exists() ||
				gjson.GetBytes(payload, "response.usage.input_tokens").Int() != 0 ||
				gjson.GetBytes(payload, "response.usage.output_tokens").Int() != 0 ||
				gjson.GetBytes(payload, "response.usage.total_tokens").Int() != 0 {
				t.Fatalf("interrupted terminal changed or restored discarded output: %s", payload)
			}
			interruptedSeen = true
			// This late duplicate precedes a new create on the wire; ordinary
			// mode splits those queues, so either processing order must be safe.
			send(control)
			send([]byte(fmt.Sprintf(`{"type":"response.create","model":%q,"instructions":"AFTER_INTERRUPT","input":[{"role":"user","content":"AFTER_INTERRUPT"}]}`, model)))
		case "response.completed":
			if gjson.GetBytes(payload, "response.id").String() == "r3" {
				if !nextCompletedSeen || gjson.GetBytes(payload, "response.output.0.content.0.text").String() != "STALE_OK" {
					t.Fatalf("response did not recover after stale interrupt: %s", payload)
				}
				if connections.Load() != 1 {
					t.Fatalf("expected same upstream socket/account, got %d connections", connections.Load())
				}
				return
			}
			if !interruptedSeen || !nextCreatedSeen || gjson.GetBytes(payload, "response.id").String() != "r2" ||
				gjson.GetBytes(payload, "response.output.0.content.0.text").String() != "NEXT_OK" ||
				bytes.Contains(payload, []byte("PARTIAL")) {
				t.Fatalf("unexpected completion after interrupt: %s", payload)
			}
			nextCompletedSeen = true
			send([]byte(fmt.Sprintf(`{"type":"response.create","model":%q,"instructions":"AFTER_STALE_INTERRUPT","input":[]}`, model)))
		case "error":
			t.Fatalf("interrupt unexpectedly rejected: %s", payload)
		}
	}
}

func TestResponsesInterruptDisabledAccountClosesIntegration(t *testing.T) {
	for _, steering := range []bool{false, true} {
		t.Run(fmt.Sprintf("steering_%t", steering), func(t *testing.T) {
			var frames atomic.Int32
			done := make(chan struct{})
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				defer close(done)
				c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
				if err != nil {
					t.Errorf("upstream upgrade: %v", err)
					return
				}
				defer func() { _ = c.Close() }()
				_ = c.SetReadDeadline(time.Now().Add(4 * time.Second))
				if _, _, errRead := c.ReadMessage(); errRead != nil {
					t.Errorf("initial upstream read: %v", errRead)
					return
				}
				frames.Add(1)
				if errWrite := c.WriteMessage(websocket.TextMessage, []byte(`{"type":"response.created","response":{"id":"disabled-active","output":[]}}`)); errWrite != nil {
					t.Errorf("upstream write: %v", errWrite)
					return
				}
				if _, payload, errRead := c.ReadMessage(); errRead == nil {
					frames.Add(1)
					t.Errorf("disabled account received another frame: %s", payload)
				} else if timeout, ok := errRead.(interface{ Timeout() bool }); ok && timeout.Timeout() {
					t.Error("disabled account upstream remained connected")
				}
			}))
			defer upstream.Close()
			cfg := &config.Config{}
			cfg.Codex.ResponseSteering, cfg.CodexResponseSteering = steering, steering
			manager, auth, c := newResponsesInterruptCloseIntegrationClient(t, cfg, upstream.URL, "true", fmt.Sprintf("interrupt-disabled-%t", steering))
			if _, payload, errRead := c.ReadMessage(); errRead != nil || gjson.GetBytes(payload, "type").String() != "response.created" {
				t.Fatalf("expected active response before disabling account: %s, %v", payload, errRead)
			}
			disabled := auth.Clone()
			disabled.Disabled, disabled.Status = true, coreauth.StatusDisabled
			if _, errUpdate := manager.Update(context.Background(), disabled); errUpdate != nil {
				t.Fatal(errUpdate)
			}
			if errWrite := c.WriteMessage(websocket.TextMessage, []byte(`{"type":"response.interrupt","response_id":"disabled-active","mode":"discard_partial_items"}`)); errWrite != nil {
				t.Fatal(errWrite)
			}
			assertResponsesInterruptClosesIntegration(t, c, false)
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Fatal("disabled account upstream close stalled")
			}
			if frames.Load() != 1 {
				t.Fatalf("disabled account forwarded %d frames; want only initial create", frames.Load())
			}
		})
	}
}

func TestResponsesInterruptHTTPTransportRequestsReconnectIntegration(t *testing.T) {
	for _, steering := range []bool{false, true} {
		t.Run(fmt.Sprintf("steering_%t", steering), func(t *testing.T) {
			var requests atomic.Int32
			var matchingSent atomic.Bool
			done := make(chan struct{})
			staleSent := make(chan struct{})
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				request := requests.Add(1)
				if request == 2 {
					defer close(done)
				}
				if request > 2 {
					t.Errorf("unexpected HTTP replay: request %d", request)
					return
				}
				body, errRead := io.ReadAll(r.Body)
				if errRead != nil {
					t.Errorf("HTTP upstream request body: %v", errRead)
					return
				}
				if bytes.Contains(body, []byte("response.interrupt")) {
					t.Error("interrupt replayed onto HTTP transport")
					return
				}
				w.Header().Set("Content-Type", "text/event-stream")
				write := func(payload string) bool {
					if _, errWrite := io.WriteString(w, "data: "+payload+"\n\n"); errWrite != nil {
						t.Errorf("HTTP upstream write: %v", errWrite)
						return false
					}
					w.(http.Flusher).Flush()
					return true
				}
				if request == 1 {
					if write(`{"type":"response.created","response":{"id":"http-previous","output":[]}}`) {
						write(`{"type":"response.completed","response":{"id":"http-previous","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"PREVIOUS_OK"}]}]}}`)
					}
					return
				}
				if !write(`{"type":"response.created","response":{"id":"http-active","output":[]}}`) {
					return
				}
				select {
				case <-staleSent:
				case <-r.Context().Done():
					t.Error("HTTP request ended before stale interrupt was sent")
					return
				case <-time.After(4 * time.Second):
					t.Error("stale HTTP interrupt was not sent")
					return
				}
				if r.Context().Err() != nil {
					t.Error("stale interrupt canceled the active HTTP request")
					return
				}
				// The response remains usable after an interrupt for the completed
				// prior ID. Only a control targeting http-active may request reconnect.
				if !write(`{"type":"response.in_progress","response":{"id":"http-active","output":[]}}`) {
					return
				}
				select {
				case <-r.Context().Done():
					if !matchingSent.Load() {
						t.Error("stale interrupt canceled HTTP before matching interrupt")
					}
				case <-time.After(4 * time.Second):
					t.Error("matching interrupted HTTP request was not canceled")
				}
			}))
			defer upstream.Close()
			cfg := &config.Config{}
			cfg.Codex.ResponseSteering, cfg.CodexResponseSteering = steering, steering
			_, _, c := newResponsesInterruptCloseIntegrationClient(t, cfg, upstream.URL, "false", fmt.Sprintf("interrupt-http-%t", steering))
			for {
				_, payload, errRead := c.ReadMessage()
				if errRead != nil {
					t.Fatalf("prior HTTP response did not complete: %v", errRead)
				}
				if gjson.GetBytes(payload, "type").String() == "response.completed" {
					if gjson.GetBytes(payload, "response.id").String() != "http-previous" {
						t.Fatalf("unexpected prior HTTP completion: %s", payload)
					}
					break
				}
			}
			if errWrite := c.WriteMessage(websocket.TextMessage, []byte(`{"type":"response.create","model":"interrupt-close-integration-model","input":[]}`)); errWrite != nil {
				t.Fatal(errWrite)
			}
			if _, payload, errRead := c.ReadMessage(); errRead != nil || gjson.GetBytes(payload, "type").String() != "response.created" || gjson.GetBytes(payload, "response.id").String() != "http-active" {
				t.Fatalf("expected active HTTP response: %s, %v", payload, errRead)
			}
			if errWrite := c.WriteMessage(websocket.TextMessage, []byte(`{"type":"response.interrupt","response_id":"http-previous","mode":"discard_partial_items"}`)); errWrite != nil {
				t.Fatal(errWrite)
			}
			close(staleSent)
			if _, payload, errRead := c.ReadMessage(); errRead != nil || gjson.GetBytes(payload, "type").String() != "response.in_progress" || gjson.GetBytes(payload, "response.id").String() != "http-active" {
				t.Fatalf("stale interrupt closed or altered active HTTP response: %s, %v", payload, errRead)
			}
			matchingSent.Store(true)
			if errWrite := c.WriteMessage(websocket.TextMessage, []byte(`{"type":"response.interrupt","response_id":"http-active","mode":"discard_partial_items"}`)); errWrite != nil {
				t.Fatal(errWrite)
			}
			assertResponsesInterruptClosesIntegration(t, c, true)
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Fatal("HTTP request cleanup stalled")
			}
			if requests.Load() != 2 {
				t.Fatalf("interrupt caused HTTP replay: %d requests; want two explicit creates", requests.Load())
			}
		})
	}
}

func newResponsesInterruptCloseIntegrationClient(t *testing.T, cfg *config.Config, upstreamURL, websockets, authID string) (*coreauth.Manager, *coreauth.Auth, *websocket.Conn) {
	t.Helper()
	manager := coreauth.NewManager(nil, nil, nil)
	manager.SetConfig(cfg)
	manager.RegisterExecutor(runtimeexecutor.NewCodexAutoExecutor(cfg))
	auth, err := manager.Register(context.Background(), &coreauth.Auth{
		ID: authID, Provider: "codex", Status: coreauth.StatusActive,
		Attributes: map[string]string{"api_key": "test-key", "base_url": upstreamURL, "websockets": websockets},
	})
	if err != nil {
		t.Fatal(err)
	}
	model := "interrupt-close-integration-model"
	registry.GetGlobalRegistry().RegisterClient(authID, "codex", []*registry.ModelInfo{{ID: model}})
	t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(authID) })
	h := NewOpenAIResponsesAPIHandler(handlers.NewBaseAPIHandlers(&cfg.SDKConfig, manager))
	router := gin.New()
	router.GET("/v1/responses", h.ResponsesWebsocket)
	downstream := httptest.NewServer(router)
	t.Cleanup(downstream.Close)
	c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(downstream.URL, "http")+"/v1/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	_ = c.SetReadDeadline(time.Now().Add(5 * time.Second))
	if errWrite := c.WriteMessage(websocket.TextMessage, []byte(fmt.Sprintf(`{"type":"response.create","model":%q,"input":[]}`, model))); errWrite != nil {
		t.Fatal(errWrite)
	}
	return manager, auth, c
}

func assertResponsesInterruptClosesIntegration(t *testing.T, c *websocket.Conn, reconnect bool) {
	t.Helper()
	for {
		_, payload, errRead := c.ReadMessage()
		if errRead != nil {
			if timeout, ok := errRead.(interface{ Timeout() bool }); ok && timeout.Timeout() {
				t.Fatal("interrupted unsupported/disabled transport remained connected")
			}
			if reconnect && !websocket.IsCloseError(errRead, websocket.CloseServiceRestart) {
				t.Fatalf("HTTP transport must request reconnect with close 1012: %v", errRead)
			}
			return
		}
		if gjson.GetBytes(payload, "type").String() != "error" || bytes.Contains(payload, []byte("unsupported websocket request type")) {
			t.Fatalf("unexpected interrupt acknowledgement or fabricated response: %s", payload)
		}
	}
}
