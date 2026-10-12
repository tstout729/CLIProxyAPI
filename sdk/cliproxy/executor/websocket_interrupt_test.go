package executor

import (
	"errors"
	"testing"
)

func TestWebsocketInterruptRegistrationLifecycle(t *testing.T) {
	interrupt := &WebsocketInterrupt{}
	if !IsUpstreamWebsocketReplayRequired(interrupt.Forward(nil)) {
		t.Fatal("unsupported transport must request replay")
	}
	old := interrupt.Register(func([]byte) error { return errors.New("old socket") })
	old(false)
	if !IsUpstreamWebsocketReplayRequired(interrupt.Forward(nil)) {
		t.Fatal("a failed attempt must not consume a later transport's interrupt")
	}
	want := errors.New("current socket")
	current := interrupt.Register(func([]byte) error { return want })
	old(true)
	if !errors.Is(interrupt.Forward(nil), want) {
		t.Fatal("stale cleanup detached a new stream")
	}
	current(true)
	current(false)
	if err := interrupt.Forward(nil); err != nil {
		t.Fatalf("late interrupt after a terminal event: %v", err)
	}
}

func TestWebsocketInterruptBlockedWriteDoesNotBlockCleanup(t *testing.T) {
	interrupt := &WebsocketInterrupt{}
	started, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	retire := interrupt.Register(func([]byte) error {
		close(started)
		<-release
		return nil
	})
	go func() {
		defer close(done)
		_ = interrupt.Forward(nil)
	}()
	<-started
	retire(true)
	close(release)
	<-done
	if err := interrupt.Forward(nil); err != nil {
		t.Fatal(err)
	}
}
