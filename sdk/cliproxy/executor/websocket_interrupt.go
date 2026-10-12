package executor

import (
	"context"
	"sync"
)

// WebsocketInterrupt binds control frames to the current stream's exact socket.
// Retired streams ignore late interrupts: their terminal event is already queued.
type WebsocketInterrupt struct {
	mu         sync.Mutex
	generation uint64
	completed  bool
	forward    func([]byte) error
}

func (w *WebsocketInterrupt) Register(forward func([]byte) error) func(bool) {
	w.mu.Lock()
	w.generation++
	generation := w.generation
	w.completed = false
	w.forward = forward
	w.mu.Unlock()
	return func(terminal bool) {
		w.mu.Lock()
		if w.generation == generation {
			w.forward = nil
			w.completed = w.completed || terminal
		}
		w.mu.Unlock()
	}
}

func (w *WebsocketInterrupt) Forward(payload []byte) error {
	w.mu.Lock()
	forward, completed := w.forward, w.completed
	w.mu.Unlock()
	if forward != nil {
		// A blocked network write must not hold the registration lock.
		return forward(payload)
	}
	if completed {
		return nil
	}
	// This transport cannot interrupt in place. Reconnect without inventing an
	// acknowledgement or replaying the control frame onto another account.
	return NewUpstreamWebsocketReplayRequiredError()
}

type websocketInterruptKey struct{}

func WithWebsocketInterrupt(ctx context.Context, interrupt *WebsocketInterrupt) context.Context {
	return context.WithValue(ctx, websocketInterruptKey{}, interrupt)
}

func WebsocketInterruptFromContext(ctx context.Context) *WebsocketInterrupt {
	if ctx == nil {
		return nil
	}
	interrupt, _ := ctx.Value(websocketInterruptKey{}).(*WebsocketInterrupt)
	return interrupt
}
