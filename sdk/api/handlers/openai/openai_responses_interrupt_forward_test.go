package openai

import (
	"bytes"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
)

func TestResponsesInterruptControlWriteFailureClosesWithoutReplay(t *testing.T) {
	gin.SetMode(gin.TestMode)
	control := []byte(`{"type":"response.interrupt","response_id":"active","mode":"discard_partial_items"}`)
	wantErr := errors.New("upstream control write failed")
	interrupts := make(chan []byte, 1)
	result := make(chan error, 1)
	canceled := make(chan error, 1)
	forwarded := make(chan []byte, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			result <- err
			return
		}
		defer func() { _ = conn.Close() }()
		c, _ := gin.CreateTestContext(w)
		c.Request = r
		data := make(chan []byte, 1)
		data <- []byte(`{"type":"response.created","response":{"id":"active"}}`)
		_, _, _, _, err = (*OpenAIResponsesAPIHandler)(nil).forwardResponsesWebsocket(
			c, newResponsesWebsocketWriter(conn), func(params ...interface{}) { canceled <- params[0].(error) },
			data, make(chan *interfaces.ErrorMessage), nil, "control-failure",
			responsesWebsocketForwardOptions{
				interrupts: interrupts,
				interrupt: func(payload []byte) error {
					forwarded <- append([]byte(nil), payload...)
					return wantErr
				},
			},
		)
		result <- err
	}))
	defer server.Close()
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, _, err = conn.ReadMessage(); err != nil {
		t.Fatalf("response.created: %v", err)
	}
	interrupts <- control
	if _, payload, errRead := conn.ReadMessage(); errRead == nil {
		t.Fatalf("control write failure must close without an acknowledgement, got %s", payload)
	}
	select {
	case err = <-result:
		if !errors.Is(err, websocket.ErrCloseSent) {
			t.Fatalf("forward result: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("forward did not exit after control write failure")
	}
	if err = <-canceled; !errors.Is(err, wantErr) {
		t.Fatalf("upstream cancellation: %v", err)
	}
	if payload := <-forwarded; !bytes.Equal(payload, control) {
		t.Fatalf("changed control payload: %s", payload)
	}
	if len(forwarded) != 0 {
		t.Fatal("control write was retried")
	}
}
