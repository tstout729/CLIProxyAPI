package auth

import (
	"fmt"
	"strings"
	"time"
)

type usageWindow uint8

const (
	usageWindowUnknown usageWindow = iota
	usageWindowWeekly
	usageWindowFiveHour
)

type usageScope uint8

const (
	usageScopeUnknown usageScope = iota
	usageScopeGlobal
	usageScopeSonnet
	usageScopeOpus
	usageScopeFable
)

type usageBlocker struct {
	window  usageWindow
	scope   usageScope
	resetAt time.Time
}

type accountUsageExhaustion struct {
	ordinal   int
	blockers  []usageBlocker
	recoverAt time.Time
}

type usageExhaustion struct {
	model    string
	total    int
	accounts []accountUsageExhaustion
}

type weeklySelection struct {
	auths []*Auth
	usage usageExhaustion
}

func usageBlockerFromBucket(bucket WeeklyQuotaBucket) usageBlocker {
	var blocker usageBlocker
	switch bucket.Window {
	case "weekly":
		blocker.window = usageWindowWeekly
	case "five-hour":
		blocker.window = usageWindowFiveHour
	}
	switch bucket.Scope {
	case "all":
		blocker.scope = usageScopeGlobal
	case "sonnet":
		blocker.scope = usageScopeSonnet
	case "opus":
		blocker.scope = usageScopeOpus
	case "fable":
		blocker.scope = usageScopeFable
	default:
		for _, family := range []struct {
			name  string
			scope usageScope
		}{{"sonnet", usageScopeSonnet}, {"opus", usageScopeOpus}, {"fable", usageScopeFable}} {
			version, ok := strings.CutPrefix(bucket.Scope, "claude-"+family.name+"-")
			if ok && version != "" && version[0] >= '0' && version[0] <= '9' && strings.IndexFunc(version, func(r rune) bool {
				return (r < '0' || r > '9') && r != '-' && r != '.'
			}) == -1 {
				blocker.scope = family.scope
				break
			}
		}
	}
	if bucket.ResetAt != nil {
		blocker.resetAt = *bucket.ResetAt
	}
	return blocker
}

func usageResetText(reset time.Time) string {
	display := reset.Truncate(time.Minute)
	if reset.After(display) {
		display = display.Add(time.Minute)
	}
	if local := weeklyPacificTime(display); local != nil {
		return local.Format("Jan 2, 2006 3:04 PM MST")
	}
	return "unknown"
}

func (u usageExhaustion) message(coolingDown bool) string {
	var message strings.Builder
	model := "the requested model"
	if u.model != "" && len(u.model) <= 128 && strings.IndexFunc(u.model, func(r rune) bool {
		return (r < 'a' || r > 'z') && (r < 'A' || r > 'Z') && (r < '0' || r > '9') && !strings.ContainsRune("-_.()", r)
	}) == -1 {
		model = "model " + u.model
	}
	if len(u.accounts) == u.total {
		if u.total == 1 {
			fmt.Fprintf(&message, "The account considered for this request has exhausted observed usage for %s. ", model)
		} else {
			fmt.Fprintf(&message, "All %d accounts considered for this request have exhausted observed usage for %s. ", u.total, model)
		}
	} else {
		availability := "unavailable"
		if coolingDown {
			availability = "cooling down"
		}
		fmt.Fprintf(&message, "Observed usage is exhausted for %d of %d accounts considered for %s. ", len(u.accounts), u.total, model)
		if remaining := u.total - len(u.accounts); remaining == 1 {
			fmt.Fprintf(&message, "The remaining account is %s; its usage window is unknown. ", availability)
		} else {
			fmt.Fprintf(&message, "The remaining %d accounts are %s; their usage window is unknown. ", remaining, availability)
		}
	}
	counts := make(map[usageWindow]int)
	var earliest time.Time
	for _, account := range u.accounts {
		seen := make(map[usageWindow]bool)
		for _, blocker := range account.blockers {
			if !seen[blocker.window] {
				counts[blocker.window]++
				seen[blocker.window] = true
			}
		}
		if !account.recoverAt.IsZero() && (earliest.IsZero() || account.recoverAt.Before(earliest)) {
			earliest = account.recoverAt
		}
	}
	fmt.Fprintf(&message, "Limits reached: weekly %d of %d; five-hour %d of %d", counts[usageWindowWeekly], u.total, counts[usageWindowFiveHour], u.total)
	if counts[usageWindowUnknown] > 0 {
		fmt.Fprintf(&message, "; unknown window %d of %d", counts[usageWindowUnknown], u.total)
	}
	message.WriteString(" (counts may overlap).")
	for _, account := range u.accounts {
		fmt.Fprintf(&message, " Account %d: ", account.ordinal)
		seen := make(map[usageBlocker]bool)
		for _, blocker := range account.blockers {
			if seen[blocker] {
				continue
			}
			if len(seen) > 0 {
				message.WriteString("; ")
			}
			seen[blocker] = true
			scope := "scope unknown"
			switch blocker.scope {
			case usageScopeGlobal:
				scope = "account-wide"
			case usageScopeSonnet:
				scope = "Sonnet"
			case usageScopeOpus:
				scope = "Opus"
			case usageScopeFable:
				scope = "Fable"
			}
			window := "usage limit (window unknown)"
			switch blocker.window {
			case usageWindowWeekly:
				window = "weekly limit"
			case usageWindowFiveHour:
				window = "five-hour limit"
			}
			fmt.Fprintf(&message, "%s %s, reset %s", scope, window, usageResetText(blocker.resetAt))
		}
		if len(seen) > 1 {
			fmt.Fprintf(&message, "; account recovery %s", usageResetText(account.recoverAt))
		}
		message.WriteByte('.')
	}
	label := "Earliest known reset"
	if coolingDown {
		label = "Earliest known usage reset"
	}
	fmt.Fprintf(&message, " %s: %s.", label, usageResetText(earliest))
	return message.String()
}

func (u usageExhaustion) decorate(err error) error {
	if err == nil || len(u.accounts) == 0 {
		return err
	}
	cooldown, isCooldown := err.(*modelCooldownError)
	detail := u.message(isCooldown && cooldown.usageRecoveryAt.IsZero())
	switch e := err.(type) {
	case *modelCooldownError:
		copyError := *e
		copyError.usageDetails = detail
		return &copyError
	case *Error:
		copyError := *e
		copyError.Message += ". " + detail
		return &copyError
	case *authUnavailableError:
		copyError, copyBase := *e, *e.base
		copyBase.Message += ". " + detail
		copyError.base = &copyBase
		return &copyError
	case *errorWithCause:
		copyError, copyBase := *e, *e.base
		copyBase.Message += ". " + detail
		copyError.base = &copyBase
		return &copyError
	case *terminalAuthError:
		copyBase := *e.base
		copyBase.Message += ". " + detail
		return NewTerminalAuthError(&copyBase, e.cause)
	default:
		return err
	}
}
