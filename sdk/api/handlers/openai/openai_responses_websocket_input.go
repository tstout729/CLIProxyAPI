package openai

import (
	"context"
	"encoding/json"

	"github.com/gorilla/websocket"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	"github.com/tidwall/gjson"
)

// readResponsesWebsocketInput is the only downstream reader. Interrupts use a
// separate queue so generation never blocks control frames in either mode.
// The bounded queue backpressures clients instead of retaining unlimited input.
func readResponsesWebsocketInput(ctx context.Context, cancel context.CancelCauseFunc, conn *websocket.Conn, interrupts chan<- []byte) <-chan cliproxyexecutor.WebsocketInput {
	input := make(chan cliproxyexecutor.WebsocketInput, 16)
	go func() {
		defer close(input)
		for {
			kind, payload, err := conn.ReadMessage()
			if err != nil {
				cancel(err)
				return
			}
			if kind != websocket.TextMessage && kind != websocket.BinaryMessage {
				continue
			}
			if interrupts != nil && json.Valid(payload) && gjson.GetBytes(payload, "type").String() == "response.interrupt" {
				select {
				case interrupts <- payload:
				case <-ctx.Done():
					return
				}
				continue
			}
			select {
			case input <- cliproxyexecutor.WebsocketInput{Payload: payload}:
			case <-ctx.Done():
				return
			}
		}
	}()
	return input
}
