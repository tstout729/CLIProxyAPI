package management

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
)

func TestWeeklyRoutingStatusSafeEmptyAndUnavailable(t *testing.T) {
	cfg := &config.Config{}
	cfg.Routing.Strategy = "weekly-reset-first"
	for _, available := range []bool{true, false} {
		var manager *coreauth.Manager
		if available {
			manager = coreauth.NewManager(nil, &coreauth.WeeklyResetFirstSelector{}, nil)
		}
		handler := NewHandler(cfg, "", manager)
		w := httptest.NewRecorder()
		ctx, _ := gin.CreateTestContext(w)
		ctx.Request = httptest.NewRequest(http.MethodGet, "/v8/management/routing/weekly-status", nil)
		handler.GetWeeklyRoutingStatus(ctx)
		if available {
			if w.Code != 200 || !strings.Contains(w.Body.String(), `"accounts":[]`) || !strings.Contains(w.Body.String(), `"strategy":"weekly-reset-first"`) {
				t.Fatalf("response = %d %s", w.Code, w.Body.String())
			}
			if w.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("quota status should not be cached")
			}
		} else if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("missing manager = %d", w.Code)
		}
	}
}
