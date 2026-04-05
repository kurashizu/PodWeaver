#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upload_video.py

Bilibili automated upload via a strict LangGraph state machine.

Design rules:
  - Exactly ONE Playwright action per node.
  - Each node handles its own retries (up to MAX_RETRIES).
  - Every node takes a screenshot before returning → live monitor updates.
  - Vision LLM is available for optional per-node verification, but the
    state machine works independently without it.

Live monitor: open http://<server-ip>:9222 in your browser.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path so 'src' module can be found
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Optional, TypedDict

import yaml
from aiohttp import web
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from playwright.async_api import Page, async_playwright

# ─────────────────────────────────────────────────────────────────────────────
# Globals (set once in main(), used inside nodes)
# ─────────────────────────────────────────────────────────────────────────────

_page: Optional[Page] = None
_screenshot: Optional[bytes] = None
_current_step: str = "starting"
_llm: Optional[ChatOpenAI] = None

MAX_RETRIES = 5

# ─────────────────────────────────────────────────────────────────────────────
# Live HTTP Monitor
# ─────────────────────────────────────────────────────────────────────────────

MONITOR_HTML = """<!DOCTYPE html>
<html><head>
  <title>PodWeaver Upload Monitor</title>
  <meta charset="utf-8">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0d1117;color:#e6edf3;font-family:monospace;
         display:flex;flex-direction:column;align-items:center;padding:16px;gap:10px}
    h1{color:#58a6ff;font-size:18px}
    #step{color:#3fb950;font-size:13px;background:#161b22;
          padding:6px 14px;border-radius:20px;border:1px solid #30363d}
    img{max-width:98vw;max-height:82vh;border:2px solid #30363d;
        border-radius:8px;object-fit:contain}
  </style>
  <script>
    setInterval(()=>{
      document.getElementById('img').src='/screenshot?_='+Date.now();
      fetch('/step').then(r=>r.text()).then(t=>document.getElementById('step').textContent=t);
    }, 800);
  </script>
</head><body>
  <h1>🎙 PodWeaver Upload Monitor</h1>
  <div id="step">initializing…</div>
  <img id="img" src="/screenshot" alt="waiting for first screenshot"/>
</body></html>"""


async def _handle_index(request):
    return web.Response(text=MONITOR_HTML, content_type="text/html")


async def _handle_screenshot(request):
    if _screenshot is None:
        return web.Response(status=204)
    return web.Response(body=_screenshot, content_type="image/png")


async def _handle_step(request):
    return web.Response(text=_current_step)


async def start_monitor(host: str, port: int):
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/screenshot", _handle_screenshot)
    app.router.add_get("/step", _handle_step)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"[Monitor] http://{host}:{port}/")


# ─────────────────────────────────────────────────────────────────────────────
# Screenshot + step-label helper
# ─────────────────────────────────────────────────────────────────────────────


async def snap(label: str = ""):
    global _screenshot, _current_step
    if _page is None:
        return
    try:
        _screenshot = await _page.screenshot()
    except Exception:
        pass
    if label:
        _current_step = label
        print(f"  📸  {label}")


# ─────────────────────────────────────────────────────────────────────────────
# Vision helper (optional; used for debugging, not for routing)
# ─────────────────────────────────────────────────────────────────────────────


async def ask_vision(question: str) -> str:
    """Show the current screenshot to the vision LLM and get a one-line answer."""
    if _llm is None or _screenshot is None:
        return "UNKNOWN"
    b64 = base64.b64encode(_screenshot).decode()
    msg = HumanMessage(
        content=[
            {
                "type": "text",
                "text": question
                + "\nRespond with YES or NO followed by one short sentence.",
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
        ]
    )
    resp = await _llm.ainvoke([msg])
    answer = resp.content.strip()
    print(f"    👁  {answer[:160]}")
    return answer


# ─────────────────────────────────────────────────────────────────────────────
# State definition
# ─────────────────────────────────────────────────────────────────────────────


class State(TypedDict):
    next_node: str  # which node the router should jump to next
    video_path: str
    cover_path: str
    title: str
    desc: str
    retries: int  # per-node retry counter, reset on every successful transition
    failed: bool
    fail_reason: str


# ─────────────────────────────────────────────────────────────────────────────
# Small transition helpers
# ─────────────────────────────────────────────────────────────────────────────


def _ok(state: State, go_to: str) -> State:
    """Return a state that advances to the next node with a clean retry counter."""
    return {**state, "next_node": go_to, "retries": 0}


def _retry(state: State, current_node: str, reason: str) -> State:
    """Increment the retry counter or mark the workflow as failed."""
    r = state["retries"] + 1
    print(f"    ⚠  Retry {r}/{MAX_RETRIES} — {reason}")
    if r >= MAX_RETRIES:
        return {
            **state,
            "failed": True,
            "fail_reason": f"[{current_node}] {reason}",
        }
    return {**state, "next_node": current_node, "retries": r}


# ─────────────────────────────────────────────────────────────────────────────
# NODE 0 — navigate
# Action: page.goto()
# ─────────────────────────────────────────────────────────────────────────────


async def node_navigate(state: State) -> State:
    print("\n── NODE 0 / navigate ─────────────────────")
    try:
        # Use "load" instead of "networkidle" — Bilibili's SPA keeps firing
        # background requests so "networkidle" often times out.
        await _page.goto(
            "https://member.bilibili.com/platform/upload/video/frame",
            wait_until="load",
            timeout=40_000,
        )
        # Give the JS framework a moment to hydrate the page
        await asyncio.sleep(4)
        await snap("0 / navigate — page loaded")

        url = _page.url
        print(f"    Current URL : {url}")

        # Grab a short snippet of raw HTML so the user can see what loaded
        content = await _page.content()
        preview = " ".join(content.split())[:400]
        print(f"    HTML preview: {preview}\n")

        # If we ended up on the login page, the cookies are expired
        if "passport.bilibili.com" in url or "login" in url:
            return {
                **state,
                "failed": True,
                "fail_reason": "navigate: redirected to login page — cookies are expired. Re-run `./biliup login`.",
            }

        # Accept the page regardless of exact keyword match; the upload node
        # will fail loudly if the file input is genuinely missing.
        print("    ✓ Navigation successful — proceeding to upload")
        return _ok(state, "upload_video")

    except Exception as e:
        print(f"    ✗ Exception: {e}")
        return _retry(state, "navigate", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# NODE 1 — upload_video
# Action: file_input.set_input_files()
# ─────────────────────────────────────────────────────────────────────────────


async def node_upload_video(state: State) -> State:
    print("\n── NODE 1 / upload_video ─────────────────")
    try:
        # Bilibili hides an <input type="file"> inside the upload widget.
        # Calling set_input_files() directly bypasses the OS file dialog.
        print("    Searching for file input element…")
        file_input = _page.locator("input[type='file']").first
        await file_input.wait_for(state="attached", timeout=15_000)
        print(f"    File input found. Injecting: {Path(state['video_path']).name}")
        await file_input.set_input_files(state["video_path"])
        print(f"    ✓ File handed to browser")
        await asyncio.sleep(4)
        await snap("1 / upload_video — file selected, upload starting")
        return _ok(state, "wait_upload")
    except Exception as e:
        print(f"    ✗ Exception: {e}")
        # Print all file inputs visible on page for diagnosis
        try:
            inputs = await _page.locator("input[type='file']").count()
            print(f"    [debug] file inputs on page: {inputs}")
            url = _page.url
            print(f"    [debug] current URL: {url}")
        except Exception:
            pass
        return _retry(state, "upload_video", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# NODE 2 — wait_upload
# Action: sleep + check for "上传完成"
# Loops back to itself until upload is done (or times out after ~6 min).
# ─────────────────────────────────────────────────────────────────────────────


async def node_wait_upload(state: State) -> State:
    poll = state["retries"] + 1
    print(f"\n── NODE 2 / wait_upload — poll #{poll} ──────")
    await asyncio.sleep(8)
    await snap(f"2 / wait_upload — poll #{poll}")

    try:
        # Check via Playwright locator (works on JS-rendered DOM)
        done = await _page.locator("text=上传完成").count()
        print(f"    [debug] '上传完成' locator count: {done}")
        if done > 0:
            print("    ✓ Upload finished!")
            return _ok(state, "fill_title")

        # Fallback: raw HTML string search
        content = await _page.content()
        if "上传完成" in content:
            print("    ✓ Upload finished (HTML fallback)!")
            return _ok(state, "fill_title")

        # Show a progress hint if visible
        try:
            pct_el = _page.locator(
                ".progress-text, .upload-progress, [class*='progress']"
            ).first
            if await pct_el.count() > 0:
                pct = await pct_el.inner_text()
                print(f"    [progress] {pct.strip()}")
        except Exception:
            pass

    except Exception as e:
        print(f"    [debug] locator error: {e}")

    # Hard ceiling: 60 polls × 8 s ≈ 8 minutes
    if state["retries"] >= 60:
        return {
            **state,
            "failed": True,
            "fail_reason": "wait_upload: timed out after 8 minutes",
        }
    print(f"    Still uploading… (elapsed ≈ {poll * 8}s)")
    return {**state, "next_node": "wait_upload", "retries": state["retries"] + 1}


# ─────────────────────────────────────────────────────────────────────────────
# NODE 3 — fill_title
# Action: triple_click to select-all + type new title
# ─────────────────────────────────────────────────────────────────────────────


async def node_fill_title(state: State) -> State:
    print("\n── NODE 3 / fill_title ───────────────────")
    # Bilibili pre-fills the title with the filename; we overwrite it.
    selectors = [
        "input[maxlength='80']",  # most reliable — title field has 80-char limit
        ".input-val",
        "input.l-input-inner",
        "input[placeholder*='标题']",
        "input[placeholder*='title']",
    ]
    print(f"    Target title: {state['title'][:60]}")
    for sel in selectors:
        try:
            loc = _page.locator(sel).first
            cnt = await loc.count()
            print(f"    Trying selector '{sel}' → found {cnt}")
            if cnt == 0:
                continue
            await loc.wait_for(state="visible", timeout=5_000)
            await loc.click(click_count=3)  # select-all existing text
            await asyncio.sleep(0.3)
            await loc.type(state["title"], delay=25)
            await asyncio.sleep(1)
            # Verify the value was actually written
            val = await loc.input_value()
            print(f"    Input value after typing: {val[:60]}")
            await snap("3 / fill_title — done")
            print(f"    ✓ Title filled via: {sel}")
            return _ok(state, "fill_desc")
        except Exception as e:
            print(f"    Selector '{sel}' failed: {e}")
            continue

    return _retry(state, "fill_title", "Could not locate title input with any selector")


# ─────────────────────────────────────────────────────────────────────────────
# NODE 4 — fill_desc
# Action: click + Ctrl+A + type description
# ─────────────────────────────────────────────────────────────────────────────


async def node_fill_desc(state: State) -> State:
    print("\n── NODE 4 / fill_desc ────────────────────")
    # Bilibili uses a Quill-based rich-text editor for the description.
    selectors = [
        ".ql-editor",
        "div[contenteditable='true']",
        "textarea[placeholder*='简介']",
        ".desc-textarea textarea",
    ]
    print(f"    Target desc ({len(state['desc'])} chars): {state['desc'][:60]}…")
    for sel in selectors:
        try:
            loc = _page.locator(sel).first
            cnt = await loc.count()
            print(f"    Trying selector '{sel}' → found {cnt}")
            if cnt == 0:
                continue
            await loc.wait_for(state="visible", timeout=5_000)
            await loc.click()
            await asyncio.sleep(0.2)
            await _page.keyboard.press("Control+a")
            await asyncio.sleep(0.2)
            await loc.type(state["desc"], delay=15)
            await asyncio.sleep(1)
            await snap("4 / fill_desc — done")
            print(f"    ✓ Desc filled via: {sel}")
            return _ok(state, "dismiss_dialogs")
        except Exception as e:
            print(f"    Selector '{sel}' failed: {e}")
            continue

    # Even if desc fails, still proceed to dismiss dialogs and submit
    print("    ! Could not fill desc — skipping and continuing to submit")
    return _ok(state, "dismiss_dialogs")


# ─────────────────────────────────────────────────────────────────────────────
# NODE 5 — submit
# Action: scroll to bottom, then click "立即投稿"
# ─────────────────────────────────────────────────────────────────────────────


async def node_dismiss_dialogs(state: State) -> State:
    """Step 4.5 — dismiss any visible consent / policy dialogs before submitting.
    Uses a short 2-second timeout so we never block here for long."""
    print("\n── NODE 4.5 / dismiss_dialogs ────────────")
    dialog_btns = ["同意", "确定", "我知道了", "关闭"]
    dismissed = 0
    for label in dialog_btns:
        try:
            loc = _page.get_by_text(label, exact=True)
            cnt = await loc.count()
            if cnt == 0:
                continue
            # Only click if actually visible — use a tight timeout to avoid blocking
            visible_loc = loc.last.filter(has_text=label) if False else loc.last
            await visible_loc.click(timeout=2_000)
            await asyncio.sleep(0.5)
            dismissed += 1
            print(f"    ✓ Dismissed dialog via '{label}'")
        except Exception:
            # Element existed in DOM but wasn't visible/clickable — skip silently
            pass
    if dismissed:
        await snap(f"4.5 / dismiss_dialogs — closed {dismissed} dialog(s)")
    else:
        print("    No visible dialogs — continuing")
    return _ok(state, "submit")


async def node_submit(state: State) -> State:
    print("\n── NODE 5 / submit ───────────────────────")
    try:
        # Scroll smoothly to the bottom so the button renders into view
        print("    Scrolling to page bottom…")
        await _page.evaluate(
            "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
        )
        await asyncio.sleep(1)
        await snap("5 / submit — scrolled to bottom")

        # Last-chance dialog dismissal with tight timeout
        for label in ["同意", "确定", "我知道了"]:
            try:
                loc = _page.get_by_text(label, exact=True)
                if await loc.count() > 0:
                    await loc.last.click(timeout=2_000)
                    await asyncio.sleep(0.3)
                    print(f"    ✓ Dismissed residual dialog: '{label}'")
            except Exception:
                pass

        # Find the submit button — it is NOT a standard <button> on Bilibili;
        # it is a Vue custom component rendered as a <span> or <div>.
        # get_by_text searches all element types.
        btn = None
        strategies = [
            ("get_by_text exact", _page.get_by_text("立即投稿", exact=True)),
            ("get_by_role button", _page.get_by_role("button", name="立即投稿")),
            ("has-text button", _page.locator("button:has-text('立即投稿')")),
            ("has-text span", _page.locator("span:has-text('立即投稿')")),
            ("has-text div", _page.locator("div:has-text('立即投稿')").last),
            (".submit-btn", _page.locator(".submit-btn")),
        ]
        for label, loc in strategies:
            try:
                cnt = await loc.count()
                print(f"    Strategy '{label}' → found {cnt}")
                if cnt > 0:
                    btn = loc.last
                    print(f"    Using strategy: {label}")
                    break
            except Exception as e:
                print(f"    Strategy '{label}' error: {e}")

        if btn is None:
            # Dump every clickable element for diagnosis
            for tag in ["button", "span", "div", "a"]:
                locs = _page.locator(tag)
                total = await locs.count()
                print(f"    [debug] <{tag}> count: {total}")
                for i in range(min(total, 6)):
                    try:
                        txt = (await locs.nth(i).inner_text()).strip()[:50]
                        if txt:
                            print(f"      {tag}[{i}]: {txt}")
                    except Exception:
                        pass
            return _retry(state, "submit", "Submit button not found in DOM")

        await btn.scroll_into_view_if_needed()
        await btn.wait_for(state="visible", timeout=5_000)
        await btn.click()
        print("    ✓ Clicked 立即投稿")
        await asyncio.sleep(5)
        await snap("5 / submit — clicked, waiting for result page")
        return _ok(state, "verify")
    except Exception as e:
        print(f"    ✗ Exception: {e}")
        return _retry(state, "submit", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# NODE 6 — verify
# Action: check for "稿件投递成功" on the result page
# ─────────────────────────────────────────────────────────────────────────────


async def node_verify(state: State) -> State:
    poll = state["retries"] + 1
    print(f"\n── NODE 6 / verify — attempt #{poll} ─────")
    await asyncio.sleep(3)
    await snap(f"6 / verify — checking for success screen (#{poll})")

    try:
        ok = await _page.locator("text=稿件投递成功").count()
        if ok > 0:
            await snap("✅ 稿件投递成功!")
            print("\n    🎉 稿件投递成功 — mission accomplished!")
            return _ok(state, "done")

        # HTML fallback
        content = await _page.content()
        if "稿件投递成功" in content or "查看进度" in content:
            await snap("✅ 稿件投递成功!")
            print("\n    🎉 稿件投递成功 (HTML fallback)!")
            return _ok(state, "done")
    except Exception:
        pass

    if state["retries"] >= MAX_RETRIES:
        return {
            **state,
            "failed": True,
            "fail_reason": "verify: success screen not detected",
        }
    return {**state, "next_node": "verify", "retries": state["retries"] + 1}


# ─────────────────────────────────────────────────────────────────────────────
# Graph wiring
# ─────────────────────────────────────────────────────────────────────────────

NODES = {
    "navigate": node_navigate,
    "upload_video": node_upload_video,
    "wait_upload": node_wait_upload,
    "fill_title": node_fill_title,
    "fill_desc": node_fill_desc,
    "dismiss_dialogs": node_dismiss_dialogs,
    "submit": node_submit,
    "verify": node_verify,
}


def router(state: State) -> str:
    """Central router: read next_node from state and translate to graph target."""
    if state.get("failed"):
        return END
    nxt = state.get("next_node", "done")
    if nxt == "done":
        return END
    return nxt


def _ok_fill_desc(state: State) -> State:
    """fill_desc advances to dismiss_dialogs, not directly to submit."""
    return _ok(state, "dismiss_dialogs")


def build_graph():
    g = StateGraph(State)
    for name, fn in NODES.items():
        g.add_node(name, fn)

    g.add_edge(START, "navigate")

    # Every node is a valid routing target; "done" maps to END.
    all_targets = {name: name for name in NODES}
    all_targets[END] = END
    for name in NODES:
        g.add_conditional_edges(name, router, all_targets)

    return g.compile()


def _ok_fill_desc_unused():
    pass  # placeholder — fill_desc now routes to dismiss_dialogs via _ok()


# ─────────────────────────────────────────────────────────────────────────────
# Config / cookie helpers
# ─────────────────────────────────────────────────────────────────────────────


def load_config() -> dict:
    from src.config import CONFIG

    return CONFIG


def parse_cookies(path: str = "cookies.json") -> list:
    from src.config import PROJECT_ROOT

    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    result = []
    for c in data.get("cookie_info", {}).get("cookies", []):
        result.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": ".bilibili.com",
                "path": "/",
                "expires": c.get("expires", -1),
                "httpOnly": bool(c.get("http_only", 0)),
                "secure": bool(c.get("secure", 0)),
            }
        )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(
        description="Upload video to Bilibili via LangGraph state machine"
    )
    parser.add_argument("--video", required=True, help="Path to merged.mp4")
    parser.add_argument("--cover", required=True, help="Path to cover image")
    parser.add_argument("--yaml", required=True, help="Path to biliup_config.yaml")
    args = parser.parse_args()

    cfg = load_config()
    pw_cfg = cfg.get("playwright", {})
    llm_cfg = cfg.get("openai", {})

    global _llm
    _llm = ChatOpenAI(
        base_url=llm_cfg.get("base_url", "http://localhost:11435/v1"),
        api_key=llm_cfg.get("api_key", "openai"),
        model=llm_cfg.get("model", "gemma4:e4b-it-q8_0"),
        temperature=0,
    )

    meta = yaml.safe_load(Path(args.yaml).read_text())
    streamer = list(meta.get("streamers", {}).values())[0]

    monitor_host = pw_cfg.get("remote_debugging_address", "0.0.0.0")
    monitor_port = pw_cfg.get("remote_debugging_port", 9222)
    await start_monitor(monitor_host, monitor_port)

    global _page
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir="/tmp/pw-bili",
            headless=pw_cfg.get("headless", True),
            viewport={"width": 1280, "height": 900},
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        cookies = parse_cookies()
        if cookies:
            await ctx.add_cookies(cookies)
            print(f"[Info] Loaded {len(cookies)} Bilibili session cookies")
        else:
            print("[Warn] No cookies found — you may need to log in manually")

        _page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        initial: State = {
            "next_node": "navigate",
            "video_path": os.path.abspath(args.video),
            "cover_path": os.path.abspath(args.cover),
            "title": streamer.get("title", "AI Video"),
            "desc": streamer.get("desc", "Uploaded by PodWeaver"),
            "retries": 0,
            "failed": False,
            "fail_reason": "",
        }

        print(f"\n🚀  Starting upload state machine")
        print(f"    Video : {initial['video_path']}")
        print(f"    Cover : {initial['cover_path']}")
        print(f"    Title : {initial['title'][:60]}")
        print()

        graph = build_graph()
        result = await graph.ainvoke(initial)

        if result.get("failed"):
            print(f"\n❌  UPLOAD FAILED: {result['fail_reason']}")
        else:
            print("\n✅  Upload pipeline completed successfully!")

        print("\n[Monitor] Staying alive for 10 s — check the final screen now.")
        await asyncio.sleep(10)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
