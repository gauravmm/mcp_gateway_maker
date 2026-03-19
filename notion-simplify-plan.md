# Notion Access Plugin Simplification Plan

## Goals

- Reduce code size and branching in `notion_access`.
- Keep the common fetch path fast.
- Keep fetch output compact by shortening large image URLs.
- Make image behavior explicit instead of relying on text-edit side effects.
- Preserve the image cache for future image download support.

## Planned Changes

### 1. Remove `hide_blocked` from `notion_access`

- Delete `hide_blocked` from `NotionAccessPluginConfig`.
- Stop assigning `self.hide_blocked = config.hide_blocked` in `NotionAccessPlugin.__init__`.
- Rely on the base plugin default of `hide_blocked = True`.

Why:
- `notion_access` is a policy-enforcement plugin, not an operator-transparency plugin.
- The adapter already hides blocked tools when `hide_blocked` is true.
- This removes one config field, one constructor branch, and related documentation and test surface.

Relevant code:
- `mcp_proxy/config/schema.py`
- `mcp_proxy/plugins/notion_access_plugin.py`
- `mcp_proxy/plugins/adapter.py`

### 2. Remove `on_list_tools` from the Notion plugin

- Keep `is_tool_allowed`.
- Delete the plugin-specific `on_list_tools` method.
- Let `PluginChainMiddleware` apply the filtering.

Why:
- The current implementation duplicates adapter behavior.
- `PluginBase` is already designed so plugins declare policy in `is_tool_allowed` and the adapter performs list filtering.

Relevant code:
- `mcp_proxy/plugins/notion_access_plugin.py`
- `mcp_proxy/plugins/base.py`
- `mcp_proxy/plugins/adapter.py`

### 3. Remove `allow_workspace_creation`

- Always require `parent.page_id` for `notion-create-pages`.
- Always inherit the parent page's first-line marker.
- Reject workspace-level page creation unconditionally.

Why:
- It simplifies the handler.
- It strengthens the permission model by ensuring every created page inherits an authoritative marker line.
- It removes a config knob that weakens the main invariant.

Tradeoff:
- This is a behavior change, not just a refactor.

Relevant code:
- `mcp_proxy/config/schema.py`
- `mcp_proxy/plugins/notion_access_plugin.py`

### 4. Keep shortened image placeholders and the image cache

- Keep `_shorten_image_urls` so `notion-fetch` returns compact placeholders instead of large S3 URLs.
- Keep `_image_cache` so the proxy retains the full image URL and block mapping for future image operations, including possible download support.
- Continue treating images as non-text state.

Why:
- This preserves the performance benefit of shortening large S3 URLs in the common read path.
- It avoids extra round trips in the common case where no image-specific action is needed.
- It keeps the current image cache, which will still be useful for future download and delete flows.

Relevant code:
- `mcp_proxy/plugins/notion_access_plugin.py`

### 5. Ban image changes through text-edit commands

- Reject `replace_content` on pages with cached images.
- Reject any `update_content` that targets, removes, or rewrites shortened image placeholders.
- Reserve image changes for dedicated image tools.
- Document that text-edit commands are text-only and do not manage image blocks.

Why:
- This avoids pretending that fetched image placeholders are a stable text representation that can safely round-trip through `update_content` or `replace_content`.
- It prevents accidental image destruction through text replacement.
- It gives the plugin a clearer contract: text tools edit text, image tools edit images.

Tradeoff:
- This is intentionally stricter.
- Agents must use dedicated image tools instead of trying to manage images through text replacement.

Relevant code:
- `mcp_proxy/plugins/notion_access_plugin.py`
- `README_NOTION.md`

### 6. Split synthetic image tooling from access-control logic

- Move `notion-upload-image` and `notion-delete-image` registration and helpers into a separate module.
- Keep `NotionAccessPlugin` focused on permission logic and marker enforcement.

Why:
- The file currently mixes access control with direct Notion API image management.
- Separating concerns will make the policy code easier to reason about and test.

Tradeoff:
- This is a structural simplification, not a behavior change.

Relevant code:
- `mcp_proxy/plugins/notion_access_plugin.py`

## Implementation Order

### Phase 1: Low-risk cleanup

- Remove `hide_blocked` from `notion_access` config and initialization.
- Remove `on_list_tools` from the Notion plugin.
- Update docs and tests for those removals.

### Phase 2: Behavior simplification

- Remove `allow_workspace_creation`.
- Require `parent.page_id` for all page creation.
- Keep first-line inheritance on child pages.

### Phase 3: Image policy hardening

- Keep `_shorten_image_urls` and `_image_cache`.
- Ban changing or destroying images through `update_content` and `replace_content`.
- Update docs so image behavior is explicit.

### Phase 4: Optional structural cleanup

- Move synthetic image tools into a separate module.

## Working Recommendation

Start with this implementation set:

- Remove `hide_blocked`.
- Remove `on_list_tools`.
- Remove `allow_workspace_creation`.
- Keep `_shorten_image_urls` and `_image_cache`.
- Ban changing or destroying images through `update_content` and `replace_content`.
- Reserve image changes for dedicated image tools.

This gives a smaller and stricter model without losing the current fetch-time performance optimization for image-heavy pages.

## Tests Most Affected

- `tests/test_notion_access_plugin.py`
- `test_update_content_targeting_first_line_blocked`
- `test_replace_content_prepends_first_line`
- image-related tests around placeholder stripping and delete behavior
- `test_create_pages_strips_duplicate_marker`
- `test_create_pages_strips_llm_marker_uses_parent`
- `test_blocked_tools_removed_from_list`

Test update notes:

- If `allow_workspace_creation` is removed, related tests should be deleted rather than updated.
- Image-related tests should be updated to assert that text-edit commands reject image mutation instead of silently stripping or preserving placeholders.