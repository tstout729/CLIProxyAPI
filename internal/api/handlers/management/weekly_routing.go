package management

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

// GetWeeklyRoutingStatus exposes only safe server-observed quota fields.
func (h *Handler) GetWeeklyRoutingStatus(c *gin.Context) {
	if h.authManager == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "auth_manager_unavailable"})
		return
	}
	h.mu.Lock()
	strategy := h.cfg.Routing.Strategy
	h.mu.Unlock()
	c.Header("Cache-Control", "no-store")
	c.JSON(http.StatusOK, gin.H{"strategy": strategy, "accounts": h.authManager.WeeklyQuotaStatus()})
}
