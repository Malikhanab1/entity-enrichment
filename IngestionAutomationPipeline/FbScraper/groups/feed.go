package groups

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"time"
)

// GetGroupFeed returns the first page of posts from a group's feed in
// reverse-chronological order.
// Returns ErrForbidden for closed or secret groups the user has not joined.
func (c *Client) GetGroupFeed(ctx context.Context, groupID string) (FeedPage, error) {
	if groupID == "" {
		return FeedPage{}, fmt.Errorf("%w: groupID must not be empty", ErrInvalidParams)
	}

	vars := mergeVars(map[string]interface{}{
		"feedLocation":                    "GROUP",
		"feedbackSource":                  69,
		"focusCommentID":                  nil,
		"privacySelectorRenderLocation":   "COMET_STREAM",
		"renderLocation":                  "groups_tab",
		"scale":                           2,
		"suppress_xac_groups":             false,
	})

	raw, err := c.graphql(ctx, "GroupsCometCrossGroupFeedContainerQuery", vars)
	if err != nil {
		return FeedPage{}, err
	}

	var data feedData
	if err := unmarshalData(raw, &data); err != nil {
		return FeedPage{}, err
	}

	page := data.toFeedPage()
	// Filter to the requested group if specified
	if groupID != "" {
		var filtered []Post
		for _, p := range page.Posts {
			if p.GroupID == groupID || p.GroupID == "" {
				filtered = append(filtered, p)
			}
		}
		if len(filtered) > 0 {
			page.Posts = filtered
		}
	}
	return page, nil
}

// GetGroupFeedPage fetches a subsequent page of group feed posts using the
// cursor returned by a prior GetGroupFeed or GetGroupFeedPage call.
// When FeedPage.HasNext is false, all available posts have been returned.
func (c *Client) GetGroupFeedPage(ctx context.Context, groupID, cursor string) (FeedPage, error) {
	if groupID == "" {
		return FeedPage{}, fmt.Errorf("%w: groupID must not be empty", ErrInvalidParams)
	}
	if cursor == "" {
		return FeedPage{}, fmt.Errorf("%w: cursor must not be empty; use GetGroupFeed for the first page", ErrInvalidParams)
	}

	vars := map[string]interface{}{
		"after":          cursor,
		"feedLocation":   "GROUP",
		"feedbackSource": 69,
		"renderLocation": "groups_tab",
		"scale":          1,
	}

	raw, err := c.graphql(ctx, "GroupsCometCrossGroupFeedPaginationQuery", vars)
	if err != nil {
		return FeedPage{}, err
	}

	var data feedPaginationData
	if err := unmarshalData(raw, &data); err != nil {
		return FeedPage{}, err
	}

	return data.toFeedPage(), nil
}

// GetGroupPosts returns posts from a single specific group (as opposed to
// [Client.GetGroupFeed] which returns the cross-group personalised feed).
//
// Use this when you want to read content from one specific group rather
// than the user's home feed across all joined groups.
func (c *Client) GetGroupPosts(ctx context.Context, groupID string) (FeedPage, error) {
	if groupID == "" {
		return FeedPage{}, fmt.Errorf("%w: groupID must not be empty", ErrInvalidParams)
	}

	vars := mergeVars(map[string]interface{}{
		"autoOpenChat":                  false,
		"creative_provider_id":          nil,
		"feedbackSource":                0,
		"feedLocation":                  "GROUP",
		"feedType":                      "DISCUSSION",
		"filter_topic_id":               nil,
		"focusCommentID":                nil,
		"groupID":                       groupID,
		"hasHoistStories":               false,
		"hoistedSectionHeaderType":      "notifications",
		"hoistStories":                  []interface{}{},
		"hoistStoriesCount":             0,
		"privacySelectorRenderLocation": "COMET_STREAM",
		"regular_stories_count":         3,
		"regular_stories_stream_initial_count": 3,
		"renderLocation":                "group",
		"scale":                         2,
		"shouldDeferMainFeed":           false,
		"sortingSetting":                "TOP_POSTS",
		"threadID":                      "",
		"useDefaultActor":               false,
	})

	raw, err := c.graphql(ctx, "CometGroupDiscussionRootSuccessQuery", vars)
	if err != nil {
		return FeedPage{}, err
	}

	var data singleFeedData
	if err := unmarshalData(raw, &data); err != nil {
		return FeedPage{}, err
	}

	return data.toFeedPage(), nil
}

// GetPost retrieves a single group post by fetching its permalink page and
// extracting the embedded Relay store data. This is more reliable than the
// GraphQL query which returns a minimal GroupPost stub with no content.
func (c *Client) GetPost(ctx context.Context, postID string) (*Post, error) {
	if postID == "" {
		return nil, fmt.Errorf("%w: postID must not be empty", ErrInvalidParams)
	}
	return c.fetchPostFromPermalink(ctx, postID, "")
}

// GetPostInGroup is like GetPost but uses the group-specific permalink URL
// (https://facebook.com/groups/{groupID}/posts/{postID}) which is more
// reliable and avoids redirects.
func (c *Client) GetPostInGroup(ctx context.Context, postID, groupID string) (*Post, error) {
	if postID == "" {
		return nil, fmt.Errorf("%w: postID must not be empty", ErrInvalidParams)
	}
	return c.fetchPostFromPermalink(ctx, postID, groupID)
}

// fetchPostFromPermalink GETs the Facebook post permalink page and extracts
// the post text and metadata from the embedded JSON in the HTML.
func (c *Client) fetchPostFromPermalink(ctx context.Context, postID, groupID string) (*Post, error) {
	// Use group-specific URL when groupID is available — it's direct with no redirect.
	url := "https://www.facebook.com/posts/" + postID
	if groupID != "" {
		url = "https://www.facebook.com/groups/" + groupID + "/posts/" + postID
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("%w: building permalink request: %v", ErrRequestFailed, err)
	}
	req.Header.Set("User-Agent", c.userAgent)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Sec-Fetch-Dest", "document")
	req.Header.Set("Sec-Fetch-Mode", "navigate")
	req.Header.Set("Sec-Fetch-Site", "none")
	req.Header.Set("Cookie", c.cookieHeader())

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: permalink GET failed: %v", ErrRequestFailed, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrNotFound
	}
	if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusUnauthorized {
		return nil, ErrForbidden
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%w: permalink returned HTTP %d", ErrRequestFailed, resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%w: reading permalink body: %v", ErrRequestFailed, err)
	}

	return parsePostFromHTML(body, postID)
}

// Regexes to extract post data from the Relay store embedded in page HTML.
var (
	// "message":{"text":"..."} — the post body
	rePostMessage = regexp.MustCompile(`"message"\s*:\s*\{"text"\s*:\s*"((?:[^"\\]|\\.)*)"\}`)
	// "creation_time":1234567890
	rePostCreationTime = regexp.MustCompile(`"creation_time"\s*:\s*(\d{10,})`)
	// "name":"Author Name" near the actors array
	rePostAuthorName = regexp.MustCompile(`"actors"\s*:\s*\[\s*\{[^}]*"name"\s*:\s*"([^"]+)"`)
	rePostAuthorID   = regexp.MustCompile(`"actors"\s*:\s*\[\s*\{[^}]*"id"\s*:\s*"(\d+)"`)
)

func parsePostFromHTML(body []byte, postID string) (*Post, error) {
	p := &Post{ID: postID}

	if m := rePostMessage.FindSubmatch(body); m != nil {
		// Unescape JSON string escapes (\\n -> \n, \\u00e9 -> é, etc.)
		p.Message = unescapeJSON(string(m[1]))
	}
	if m := rePostCreationTime.FindSubmatch(body); m != nil {
		if ts, err := strconv.ParseInt(string(m[1]), 10, 64); err == nil {
			p.CreatedAt = time.Unix(ts, 0).UTC()
		}
	}
	if m := rePostAuthorName.FindSubmatch(body); m != nil {
		p.AuthorName = unescapeJSON(string(m[1]))
	}
	if m := rePostAuthorID.FindSubmatch(body); m != nil {
		p.AuthorID = string(m[1])
	}

	fmt.Printf("[DEBUG parsePostFromHTML] post=%s message=%q createdAt=%v author=%s\n",
		postID, p.Message, p.CreatedAt, p.AuthorName)

	return p, nil
}

// unescapeJSON converts a JSON-encoded string value (without the surrounding
// quotes) to a plain Go string.
func unescapeJSON(s string) string {
	// Wrap in quotes and let json.Unmarshal handle all escape sequences.
	quoted := `"` + s + `"`
	var out string
	if err := json.Unmarshal([]byte(quoted), &out); err != nil {
		// Fallback: return as-is if unmarshal fails (malformed escape).
		return s
	}
	return out
}
