package openai

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
	"github.com/tidwall/gjson"
)

func interruptToolCacheItem(kind, callID, content string) []byte {
	item := map[string]string{"type": kind, "call_id": callID, "name": "lookup"}
	if kind == "function_call" {
		item["arguments"] = content
	} else {
		item["input"] = content
	}
	raw, _ := json.Marshal(item)
	return raw
}

func interruptToolCacheEvent(item []byte) []byte {
	return []byte(fmt.Sprintf(`{"type":"response.output_item.done","item":%s}`, item))
}

func TestResponsesInterruptDiscardsOnlyEmittedToolCalls(t *testing.T) {
	for _, staged := range []bool{false, true} {
		for _, kind := range []string{"function_call", "custom_tool_call"} {
			t.Run(fmt.Sprintf("staged_%t/%s", staged, kind), func(t *testing.T) {
				sessionKey := t.Name()
				defer defaultWebsocketToolCallCache.deleteSession(sessionKey)
				defer defaultWebsocketToolOutputCache.deleteSession(sessionKey)
				var turn *responsesWebsocketToolCacheTurn
				if staged {
					turn = newResponsesWebsocketToolCacheTurn(sessionKey)
				}
				write := func(id string, raw []byte) {
					if turn != nil {
						turn.recordRawItem(kind, id, raw)
					} else {
						defaultWebsocketToolCallCache.record(sessionKey, id, raw)
					}
				}
				prior := interruptToolCacheItem(kind, "prior", "PREVIOUS_INPUT")
				write("prior", prior)
				globalPrior := interruptToolCacheItem(kind, "global-prior", "PREVIOUS_CACHE")
				defaultWebsocketToolCallCache.record(sessionKey, "global-prior", globalPrior)
				tracker := newResponsesWebsocketResponseToolCalls(sessionKey, turn)
				tracker.record([]byte(`{"type":"response.created","response":{"id":"r1"}}`))
				for _, id := range []string{"discarded", "prior", "global-prior", "retained", "changed"} {
					tracker.record(interruptToolCacheEvent(interruptToolCacheItem(kind, id, "PARTIAL")))
					// Repeated item updates must preserve the original snapshot.
					tracker.record(interruptToolCacheEvent(interruptToolCacheItem(kind, id, "LATEST_PARTIAL")))
				}
				changed := interruptToolCacheItem(kind, "changed", "OTHER_WRITER")
				write("changed", changed)
				retained := interruptToolCacheItem(kind, "retained", "AUTHORITATIVE")
				terminalOnly := interruptToolCacheItem(kind, "terminal-only", "TERMINAL_ONLY")
				tracker.discard([]byte(fmt.Sprintf(`{"type":"response.incomplete","response":{"id":"r1","incomplete_details":{"reason":"interrupted"},"output":[%s,%s]}}`, retained, terminalOnly)))
				if turn != nil {
					turn.commit()
				}
				if _, exists := defaultWebsocketToolCallCache.get(sessionKey, "discarded"); exists {
					t.Fatal("discarded response call remained available to transcript repair")
				}
				for id, want := range map[string][]byte{
					"prior": prior, "global-prior": globalPrior, "retained": retained,
					"terminal-only": terminalOnly, "changed": changed,
				} {
					got, exists := defaultWebsocketToolCallCache.get(sessionKey, id)
					if !exists || !bytes.Equal(got, want) {
						t.Fatalf("cached call %s = %s, want %s", id, got, want)
					}
				}
				outputKind := "function_call_output"
				if kind == "custom_tool_call" {
					outputKind = "custom_tool_call_output"
				}
				request := []byte(fmt.Sprintf(`{"input":[{"type":%q,"call_id":"discarded","output":"DISCARDED_RESULT"},{"type":%q,"call_id":"prior","output":"PRIOR_RESULT"},{"type":%q,"call_id":"retained","output":"RETAINED_RESULT"}]}`, outputKind, outputKind, outputKind))
				repaired := repairResponsesWebsocketToolCallsWithCaches(defaultWebsocketToolOutputCache, defaultWebsocketToolCallCache, sessionKey, request)
				items := gjson.GetBytes(repaired, "input").Array()
				if len(items) != 4 || bytes.Contains(repaired, []byte("discarded")) ||
					gjson.GetBytes(repaired, "input.0.call_id").String() != "prior" ||
					gjson.GetBytes(repaired, "input.2.call_id").String() != "retained" {
					t.Fatalf("repair resurrected discarded calls or lost retained calls: %s", repaired)
				}
			})
		}
	}
}

func TestResponsesInterruptToolCallTrackerResetsForEachResponse(t *testing.T) {
	for _, staged := range []bool{false, true} {
		t.Run(fmt.Sprintf("staged_%t", staged), func(t *testing.T) {
			sessionKey := t.Name()
			defer defaultWebsocketToolCallCache.deleteSession(sessionKey)
			var turn *responsesWebsocketToolCacheTurn
			if staged {
				turn = newResponsesWebsocketToolCacheTurn(sessionKey)
			}
			tracker := newResponsesWebsocketResponseToolCalls(sessionKey, turn)
			previous := interruptToolCacheItem("function_call", "previous", "COMPLETED")
			tracker.record([]byte(`{"type":"response.created","response":{"id":"r1"}}`))
			tracker.record(interruptToolCacheEvent(previous))
			tracker.record([]byte(`{"type":"response.created","response":{"id":"r2"}}`))
			tracker.record(interruptToolCacheEvent(interruptToolCacheItem("function_call", "previous", "PARTIAL_REPLACEMENT")))
			tracker.record(interruptToolCacheEvent(interruptToolCacheItem("function_call", "new", "PARTIAL")))
			tracker.discard([]byte(`{"type":"response.incomplete","response":{"id":"r2","incomplete_details":{"reason":"interrupted"},"output":[]}}`))
			if turn != nil {
				turn.commit()
			}
			if got, exists := defaultWebsocketToolCallCache.get(sessionKey, "previous"); !exists || !bytes.Equal(got, previous) {
				t.Fatalf("previous response call = %s, want %s", got, previous)
			}
			if _, exists := defaultWebsocketToolCallCache.get(sessionKey, "new"); exists {
				t.Fatal("interrupted response retained its new call")
			}
		})
	}
}

func TestResponsesInterruptForwardDiscardsToolCallCache(t *testing.T) {
	for _, staged := range []bool{false, true} {
		t.Run(fmt.Sprintf("staged_%t", staged), func(t *testing.T) {
			sessionKey := t.Name()
			defer defaultWebsocketToolCallCache.deleteSession(sessionKey)
			defer defaultWebsocketToolOutputCache.deleteSession(sessionKey)
			terminal := []byte(`{"type":"response.incomplete","response":{"id":"r1","incomplete_details":{"reason":"interrupted"},"output":[]}}`)
			result := make(chan error, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := responsesWebsocketUpgrader.Upgrade(w, r, nil)
				if err != nil {
					result <- err
					return
				}
				defer func() { _ = conn.Close() }()
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = r
				data := make(chan []byte, 4)
				data <- []byte(`{"type":"response.created","response":{"id":"r1"}}`)
				data <- interruptToolCacheEvent(interruptToolCacheItem("function_call", "discarded-function", "PARTIAL"))
				data <- interruptToolCacheEvent(interruptToolCacheItem("custom_tool_call", "discarded-custom", "PARTIAL"))
				data <- terminal
				close(data)
				var turn *responsesWebsocketToolCacheTurn
				if staged {
					turn = newResponsesWebsocketToolCacheTurn(sessionKey)
				}
				output, responseID, pending, errMsg, errForward := (*OpenAIResponsesAPIHandler)(nil).forwardResponsesWebsocket(
					c, newResponsesWebsocketWriter(conn), func(...interface{}) {}, data, make(chan *interfaces.ErrorMessage), nil, sessionKey,
					responsesWebsocketForwardOptions{toolCacheTurn: turn},
				)
				if errForward != nil {
					result <- errForward
					return
				}
				if errMsg != nil || string(output) != "[]" || responseID != "r1" || len(pending) != 0 {
					result <- fmt.Errorf("interrupted forward retained discarded output: output=%s response=%s pending=%v error=%v", output, responseID, pending, errMsg)
					return
				}
				if turn != nil {
					turn.commit()
				}
				result <- nil
			}))
			defer server.Close()
			conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), http.Header{"Session-Id": []string{sessionKey}})
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = conn.Close() }()
			_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
			for i := 0; i < 4; i++ {
				_, payload, errRead := conn.ReadMessage()
				if errRead != nil {
					t.Fatal(errRead)
				}
				if i == 3 && !bytes.Equal(payload, terminal) {
					t.Fatalf("interrupted terminal restored discarded items: %s", payload)
				}
			}
			select {
			case err = <-result:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("interrupted forward did not finish")
			}
			request := []byte(`{"input":[{"type":"function_call_output","call_id":"discarded-function","output":"RESULT"},{"type":"custom_tool_call_output","call_id":"discarded-custom","output":"RESULT"}]}`)
			repaired := repairResponsesWebsocketToolCallsWithCaches(defaultWebsocketToolOutputCache, defaultWebsocketToolCallCache, sessionKey, request)
			if got := gjson.GetBytes(repaired, "input").Raw; got != "[]" {
				t.Fatalf("forward left discarded calls available for fallback repair: %s", repaired)
			}
		})
	}
}
