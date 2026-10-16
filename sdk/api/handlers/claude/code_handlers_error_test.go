package claude

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/api/handlers"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	"github.com/tidwall/gjson"
)

func TestClaudeErrorExtractsOpenAIStyleUpstreamJSON(t *testing.T) {
	handler := &ClaudeCodeAPIHandler{}
	msg := &interfaces.ErrorMessage{
		StatusCode: http.StatusBadRequest,
		Error:      errors.New(`{"error":{"message":"Your input exceeds the context window of this model. Please adjust your input and try again.","type":"invalid_request_error","code":"context_too_large"}}`),
	}

	got := handler.toClaudeError(msg)

	if got.Type != "error" {
		t.Fatalf("type = %q, want error", got.Type)
	}
	if got.Error.Type != "invalid_request_error" {
		t.Fatalf("error.type = %q, want invalid_request_error", got.Error.Type)
	}
	if got.Error.Message != "Your input exceeds the context window of this model. Please adjust your input and try again." {
		t.Fatalf("error.message = %q", got.Error.Message)
	}
}

func TestClaudeErrorExtractsClaudeStyleUpstreamJSON(t *testing.T) {
	handler := &ClaudeCodeAPIHandler{}
	msg := &interfaces.ErrorMessage{
		StatusCode: http.StatusTooManyRequests,
		Error:      errors.New(`{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account's rate limit. Please try again later."},"request_id":"req_123"}`),
	}

	got := handler.toClaudeError(msg)

	if got.Error.Type != "rate_limit_error" {
		t.Fatalf("error.type = %q, want rate_limit_error", got.Error.Type)
	}
	if got.Error.Message != "This request would exceed your account's rate limit. Please try again later." {
		t.Fatalf("error.message = %q", got.Error.Message)
	}
}

func TestWriteClaudeErrorResponseUsesClaudeEnvelope(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	handler := &ClaudeCodeAPIHandler{}
	msg := &interfaces.ErrorMessage{
		StatusCode: http.StatusBadRequest,
		Error:      errors.New(`{"error":{"message":"Your input exceeds the context window of this model. Please adjust your input and try again.","type":"invalid_request_error","code":"context_too_large"}}`),
	}

	handler.WriteErrorResponse(c, msg)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusBadRequest)
	}
	body := recorder.Body.Bytes()
	if got := gjson.GetBytes(body, "type").String(); got != "error" {
		t.Fatalf("type = %q, want error; body=%s", got, body)
	}
	if got := gjson.GetBytes(body, "error.type").String(); got != "invalid_request_error" {
		t.Fatalf("error.type = %q, want invalid_request_error; body=%s", got, body)
	}
	if got := gjson.GetBytes(body, "error.message").String(); got != "Your input exceeds the context window of this model. Please adjust your input and try again." {
		t.Fatalf("error.message = %q; body=%s", got, body)
	}
}

func TestWriteClaudeErrorResponse_IncludesRetryAfterForModelCooldownDefaultSettings(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	handler := &ClaudeCodeAPIHandler{}

	cooldownErr := coreauth.NewManager(nil, nil, nil)
	_ = cooldownErr
	// Create mock model cooldown error
	msg := &interfaces.ErrorMessage{
		StatusCode: http.StatusTooManyRequests,
		Error:      coreauth.NewModelCooldownError("claude-sonnet-4-6", "claude", 20*time.Second),
	}

	handler.WriteErrorResponse(c, msg)

	if recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusTooManyRequests)
	}
	if got := recorder.Header().Get("Retry-After"); got != "20" {
		t.Fatalf("Retry-After = %q, want 20", got)
	}
	// Claude Code's unattended retry mode waits until this Unix time.
	reset, err := strconv.ParseInt(recorder.Header().Get(coreauth.HeaderAnthropicRatelimitUnifiedReset), 10, 64)
	if wait := time.Until(time.Unix(reset, 0)); err != nil || wait < 18*time.Second || wait > 22*time.Second {
		t.Fatalf("%s = %q, want about 20s from now", coreauth.HeaderAnthropicRatelimitUnifiedReset, recorder.Header().Get(coreauth.HeaderAnthropicRatelimitUnifiedReset))
	}
}

func TestPendingClaudeStreamErrorUsesBufferedError(t *testing.T) {
	wantErr := &interfaces.ErrorMessage{
		StatusCode: http.StatusBadRequest,
		Error:      errors.New(`{"error":{"message":"Your input exceeds the context window of this model. Please adjust your input and try again.","type":"invalid_request_error","code":"context_too_large"}}`),
	}
	errs := make(chan *interfaces.ErrorMessage, 1)
	errs <- wantErr
	close(errs)

	gotErr, ok := handlers.PendingStreamError(errs)
	if !ok {
		t.Fatal("expected pending stream error")
	}
	if gotErr != wantErr {
		t.Fatalf("pending error = %p, want %p", gotErr, wantErr)
	}
}

func TestWriteClaudeErrorResponseRetainsUsageLimitBreakdown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	message := "All 4 accounts considered for this request have exhausted observed usage for model claude-sonnet-4.5. Limits reached: weekly 3 of 4; five-hour 1 of 4 (counts may overlap). Account 1: account-wide weekly limit, reset Oct 16, 2026 2:00 AM PDT. Account 2: account-wide weekly limit, reset Oct 16, 2026 3:00 AM PDT. Account 3: account-wide weekly limit, reset Oct 16, 2026 4:00 AM PDT. Account 4: account-wide five-hour limit, reset Oct 16, 2026 5:00 AM PDT. Earliest known reset: Oct 16, 2026 2:00 AM PDT."
	payload, err := json.Marshal(map[string]any{"error": map[string]any{"code": "model_cooldown", "message": message, "reset_seconds": 7200, "reset_at": "2026-10-16T02:00:00-07:00"}})
	if err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	(&ClaudeCodeAPIHandler{}).WriteErrorResponse(c, &interfaces.ErrorMessage{StatusCode: http.StatusTooManyRequests, Error: errors.New(string(payload))})
	if recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", recorder.Code)
	}
	if got := gjson.GetBytes(recorder.Body.Bytes(), "error.message").String(); got != message {
		t.Fatalf("Claude message lost usage breakdown: %s", got)
	}
	if got := gjson.GetBytes(recorder.Body.Bytes(), "error.type").String(); got != "rate_limit_error" {
		t.Fatalf("error.type = %q", got)
	}
}
