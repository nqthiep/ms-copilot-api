"""Direct client for the M365 Copilot chat backend.

The web app at copilot.cloud.microsoft/chat talks to a SignalR-over-WebSocket
service; this module speaks that wire protocol directly, so the gateway needs
no browser at request time:

* endpoint: ``wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tid}``
  (oid/tid come from the access token's JWT claims; the token itself rides in
  the URL query string)
* every frame is JSON terminated by a 0x1E (record separator) byte
* handshake: ``{"protocol":"json","version":1}`` -> server replies ``{}``
* one turn = chat invocation (type 4, target "chat") + a Metrics frame
  (type 1, target "Metrics") in a single WebSocket send; without the Metrics
  frame the turn silently produces nothing
* the model is picked by a ``tone`` string, not a model id
* one WebSocket per turn; the same session/conversation ids keep server-side
  context across turns

Server-to-client streaming mixes token deltas with full-text snapshots, so
``fold_text`` only ever emits a true prefix of the growing answer.
"""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import websockets

from copilot_gateway.provider.errors import AuthRequired, ProviderTimeout, ProviderUnavailable
from copilot_gateway.utils.logging import get_logger

log = get_logger("sydney")

RS = "\x1E"
CHATHUB_URL = "wss://substrate.office.com/m365Copilot/Chathub"
UPLOAD_URL = "https://substrate.office.com/m365Copilot/UploadFile"
ORIGIN = "https://m365.cloud.microsoft"

WS_HEADERS = {
    "Origin": ORIGIN,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Model id -> tone. The server validates tones, so unknown ids fall back to the
# default tone instead of inventing one.
MODEL_TONES = {
    "m365-copilot": "magic",
    "copilot-web": "magic",
    "auto": "magic",
    "m365-quick": "Gpt_5_5_Chat",
    "gpt-5.5": "Gpt_5_5_Chat",
    "gpt-5.5-quick": "Gpt_5_5_Chat",
    "m365-think-deeper": "Gpt_5_5_Reasoning",
    "gpt-5.5-think-deeper": "Gpt_5_5_Reasoning",
    "gpt-5.6-think-deeper": "Gpt_5_6_Reasoning",
    "claude-sonnet": "Claude_Sonnet",
    "claude": "Claude_Sonnet",
}

AVAILABLE_MODELS = [
    "m365-copilot",
    "copilot-web",
    "m365-quick",
    "m365-think-deeper",
    "claude-sonnet",
]


def tone_for_model(model: str, default: str = "magic") -> str:
    if model in MODEL_TONES:
        return MODEL_TONES[model]
    if model.lower().startswith("claude"):
        return "Claude_Sonnet"
    return default


# Feature flags captured from the web client's own session. Removing them is
# untested, so the proven list is sent as-is.
VARIANTS = ",".join(
    [
        "EnableMcpServerWidgets",
        "feature.EnableMcpServerWidgets",
        "feature.EnableLuForChatCIQ",
        "feature.enableChatCIQPlugin",
        "EnableRequestPlugins",
        "feature.EnableSensitivityLabels",
        "EnableUnsupportedUrlDetector",
        "feature.IsCustomEngineCopilotEnabled",
        "feature.bizchatfluxv3",
        "feature.enablechatpages",
        "feature.enableCodeCanvas",
        "feature.turnOnWorkTabRecommendation",
        "turnOffWorkTabUpsellFromClient",
        "feature.turnOnDARecommendation",
        "feature.IsStreamingModeInChatRequestEnabled",
        "IncludeSourceAttributionsConcise",
        "SkipPublishEmptyMessage",
        "feature.EnableDeduplicatingSourceAttributions",
        "Enable3PActionProgressMessages",
        "feature.enableClientWebRtc",
        "feature.EnableMeetingRecapOfSeriesMeetingWithCiq",
        "feature.EnableReferencesListCompleteSignal",
        "feature.StorageMessageSplitDisabled",
        "feature.EnableCuaTakeControlApi",
        "feature.cwcallowedos",
        "feature.disabledisallowedmsgs",
        "feature.enableCitationsForSynthesisData",
        "feature.enableGenerateGraphicArtOptionsSet",
        "cdximagen",
        "feature.EnableUpdatedUXForConfirmationDialog",
        "feature.EnableClientFileURLSupportForOfficeWebPaidCopilot",
        "feature.EnableDesignEditorImageGrounding",
        "feature.EnableDesignerEditor",
        "feature.OfficeWebToHelix",
        "feature.OfficeDesktopToHelix",
        "feature.M365TeamsHubToHelix",
        "feature.OwaHubToHelix",
        "feature.MonarchHubToHelix",
        "feature.Win32OutlookHubToHelix",
        "feature.MacOutlookHubToHelix",
        "Agt_bizchat_enableGpt5ForHelix",
    ]
)

# Server-side Python sandbox (the model runs code and returns real results).
CODE_INTERPRETER_OPTIONS_SETS = [
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "code_interpreter_matplotlib_patching",
]

# Required for an attached image to actually be processed.
ATTACHMENT_OPTIONS_SETS = [
    "cwc_flux_image",
    "cwcfluxgptv",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
    "cwc_fileupload_odb",
    "add_filestore_filetype",
]

# Real "Stop generating" frame, as the web client sends it on abort.
STOP_FRAME = json.dumps({"arguments": [{}], "invocationId": "1", "target": "stop", "type": 1}) + RS


class SydneyError(Exception):
    """The ChatHub reported a protocol-level failure."""


class EmptyResponse(Exception):
    """Copilot finished without producing any text."""


class DisengagedError(EmptyResponse):
    """Copilot's safety filter refused the prompt (messageType Disengaged)."""


@dataclass
class UploadedImage:
    doc_id: str
    file_type: str
    file_name: str


@dataclass
class _TurnResult:
    answer: str
    disengaged: bool


def split_frames(data: str) -> list[str]:
    """Split one WebSocket message into its 0x1E-terminated JSON frames."""
    return [frame for frame in data.split(RS) if frame]


def fold_text(current: str, nxt: str) -> tuple[str, str | None]:
    """Fold a streamed snapshot/delta into the running answer.

    Returns (new_answer, text_to_emit). Only a true prefix of the answer is
    ever emitted, because already-emitted bytes cannot be retracted.
    """
    if len(nxt) <= len(current):
        return current, None
    if nxt.startswith(current):
        return nxt, nxt[len(current):]
    return nxt, None


def build_ws_url(
    *,
    oid: str,
    tid: str,
    session_id: str,
    conversation_id: str,
    token: str,
    request_id: str,
    variants: str = VARIANTS,
) -> str:
    params = {
        "chatsessionid": request_id,
        "clientrequestid": request_id,
        "X-SessionId": session_id,
        "ConversationId": conversation_id,
        "access_token": token,
        "variants": variants,
        # The web client sends source double-quoted; keep it identical.
        "source": '"officeweb"',
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": "Starter",
        "agent": "web",
        "scenario": "OfficeWebIncludedCopilot",
    }
    return f"{CHATHUB_URL}/{oid}@{tid}?{urlencode(params)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_chat_invocation(
    *,
    text: str,
    tone: str,
    request_id: str,
    session_id: str,
    is_first_turn: bool,
    annotations: list[dict] | None = None,
    code_interpreter: bool = True,
) -> dict:
    options_sets = list(CODE_INTERPRETER_OPTIONS_SETS) if code_interpreter else []
    if annotations:
        options_sets += ATTACHMENT_OPTIONS_SETS
    message: dict = {
        "author": "user",
        "inputMethod": "Keyboard",
        "text": text,
        "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
        "requestId": request_id,
        "locationInfo": {"timeZoneOffset": 0, "timeZone": "UTC"},
        "locale": "en-gb",
        "messageType": "Chat",
        "experienceType": "Default",
        "adaptiveCards": [],
        "clientPreferences": {},
    }
    if annotations:
        message["messageAnnotations"] = annotations
    return {
        "arguments": [
            {
                "source": "officeweb",
                "clientCorrelationId": request_id,
                "sessionId": session_id,
                "optionsSets": options_sets,
                "streamingMode": "ConciseWithPadding",
                "spokenTextMode": "None",
                "options": {},
                "extraExtensionParameters": {},
                "allowedMessageTypes": [
                    "Chat",
                    "Suggestion",
                    "InternalSearchQuery",
                    "Disengaged",
                    "InternalLoaderMessage",
                    "Progress",
                    "RenderCardRequest",
                    "SemanticSerp",
                    "GenerateContentQuery",
                    "SearchQuery",
                    "ConfirmationCard",
                    "DeveloperLogs",
                    "EndOfRequest",
                    "ReferencesListComplete",
                    "GeneratedCode",
                ],
                "sliceIds": [],
                "threadLevelGptId": {},
                "traceId": request_id,
                "isStartOfSession": is_first_turn,
                "clientInfo": {
                    "clientPlatform": "mcmcopilot-web",
                    "clientAppName": "Office",
                    "clientEntrypoint": "mcmcopilot-officeweb",
                    "clientSessionId": session_id,
                    "clientAppType": "Web",
                    "deviceOS": "MacOSX",
                    "deviceType": "Desktop",
                },
                "message": message,
                "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                "isSbsSupported": True,
                "tone": tone,
                "renderReferencesBehindEOS": True,
                "disconnectBehavior": "continue",
            }
        ],
        "invocationId": "0",
        "target": "chat",
        "type": 4,
    }


def build_metrics_frame() -> dict:
    now = _now_iso()
    return {
        "arguments": [
            {
                "Timestamps": {
                    "ConnectionStart": now,
                    "UserInputStart": now,
                    "ConnectionEstablished": now,
                    "UserInputSubmit": now,
                }
            }
        ],
        "target": "Metrics",
        "type": 1,
    }


def image_annotations(uploads: list[UploadedImage]) -> list[dict]:
    return [
        {
            "id": upload.doc_id,
            "messageAnnotationMetadata": {
                "@type": "File",
                "annotationType": "File",
                "fileType": upload.file_type,
                "fileName": upload.file_name,
            },
            "messageAnnotationType": "ImageFile",
        }
        for upload in uploads
    ]


async def upload_image(
    http: httpx.AsyncClient,
    *,
    token: str,
    conversation_id: str,
    filename: str,
    data: bytes,
    mime: str,
) -> UploadedImage:
    """Upload an image the way the web client does, returning its document id."""
    payload = base64.b64encode(data).decode("ascii")
    # The client sends optionsSets twice; urlencode a list of pairs to match.
    body = urlencode(
        [
            ("scenario", "UploadImage"),
            ("conversationId", conversation_id),
            ("FileBase64", f"data:{mime};base64,{payload}"),
            ("optionsSets", "cwcgptvsan"),
            ("optionsSets", "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch"),
        ]
    )
    response = await http.post(
        UPLOAD_URL,
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Scenario": "OfficeWebIncludedCopilot",
            "X-Variants": "feature.EnableImageSupportInUploadFile",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/chat",
        },
    )
    if response.status_code == 401:
        raise AuthRequired("Image upload rejected the access token; sign in again")
    try:
        result = response.json()
    except ValueError as exc:
        raise SydneyError(f"Image upload failed (HTTP {response.status_code})") from exc
    doc_id = result.get("docId") or result.get("doc_id") or result.get("id")
    if response.status_code != 200 or not doc_id:
        raise SydneyError(f"Image upload failed (HTTP {response.status_code}): {result}")
    return UploadedImage(
        doc_id=str(doc_id),
        file_type=mime.split("/", 1)[1].split("+")[0],
        file_name=filename,
    )


@dataclass
class SydneyConversation:
    """One server-side conversation. A new WebSocket is opened per turn, but the
    ids are reused so Copilot keeps the thread context."""

    session_id: str = ""
    conversation_id: str = ""
    turn_count: int = 0
    resumed: bool = False

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        if not self.conversation_id:
            self.conversation_id = str(uuid.uuid4())

    @property
    def is_first_turn(self) -> bool:
        # isStartOfSession is true only on the first turn of a conversation we
        # created; a resumed conversation must send false.
        return self.turn_count == 0 and not self.resumed


async def stream_chat_turn(
    *,
    token: str,
    claims: dict,
    conversation: SydneyConversation,
    text: str,
    tone: str,
    images: list[UploadedImage] | None = None,
    timeout: float = 300,
    code_interpreter: bool = True,
    proxy: str | None = None,
) -> AsyncIterator[str]:
    """Send one turn and yield the reply's text deltas as they stream in."""
    request_id = str(uuid.uuid4())
    url = build_ws_url(
        oid=claims["oid"],
        tid=claims["tid"],
        session_id=conversation.session_id,
        conversation_id=conversation.conversation_id,
        token=token,
        request_id=request_id,
    )
    conversation.turn_count += 1

    queue: asyncio.Queue[str | BaseException | _TurnResult] = asyncio.Queue()

    async def pump() -> None:
        answer = ""
        disengaged = False
        handshake_done = asyncio.Event()
        needs_pong = asyncio.Event()

        def advance(nxt: str) -> None:
            nonlocal answer
            answer, emit = fold_text(answer, nxt)
            if emit:
                queue.put_nowait(emit)

        def note_bot(message: dict) -> None:
            nonlocal disengaged
            if message.get("messageType") == "Disengaged":
                disengaged = True
            content = message.get("text")
            if message.get("author") == "bot" and content and not message.get("messageType"):
                advance(content)

        def handle(frame: dict) -> bool:
            """Process one parsed frame. Returns False when the turn is over."""
            frame_type = frame.get("type")
            if frame_type == 7:  # close
                if frame.get("error"):
                    queue.put_nowait(SydneyError(f"Server closed the turn: {frame['error']}"))
                return False
            if frame_type == 3:  # completion
                if frame.get("error"):
                    queue.put_nowait(SydneyError(f"Turn failed: {frame['error']}"))
                return False
            if frame_type == 2:  # stream item: final conversation state
                item = frame.get("item") or {}
                for message in item.get("messages", []):
                    note_bot(message)
                return False
            if frame_type == 1 and frame.get("target") == "update":
                for arg in frame.get("arguments", []):
                    if isinstance(arg.get("writeAtCursor"), str):
                        advance(answer + arg["writeAtCursor"])
                    elif isinstance(arg.get("messages"), list):
                        for message in arg["messages"]:
                            note_bot(message)
            return True

        async def sender(ws) -> None:  # noqa: ANN001
            await ws.send(json.dumps({"protocol": "json", "version": 1}) + RS)
            await handshake_done.wait()
            invocation = build_chat_invocation(
                text=text,
                tone=tone,
                request_id=request_id,
                session_id=conversation.session_id,
                is_first_turn=conversation.is_first_turn,
                annotations=image_annotations(images or []),
                code_interpreter=code_interpreter,
            )
            # The chat invocation and the Metrics frame go out in ONE send, or
            # the turn silently produces nothing.
            await ws.send(json.dumps(invocation) + RS + json.dumps(build_metrics_frame()) + RS)
            while True:
                await needs_pong.wait()
                needs_pong.clear()
                try:
                    await ws.send(json.dumps({"type": 6}) + RS)
                except Exception:
                    return

        try:
            connect_kwargs: dict = {"additional_headers": WS_HEADERS}
            if proxy:
                connect_kwargs["proxy"] = proxy
            async with websockets.connect(url, **connect_kwargs) as ws:
                sender_task = asyncio.create_task(sender(ws))
                try:
                    async for raw in ws:
                        text_msg = (
                            raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                        )
                        for frame_text in split_frames(text_msg):
                            try:
                                frame = json.loads(frame_text)
                            except ValueError:
                                # The empty handshake acknowledgement.
                                handshake_done.set()
                                continue
                            if not handshake_done.is_set():
                                handshake_done.set()
                                if frame.get("error"):
                                    queue.put_nowait(
                                        SydneyError(f"Handshake failed: {frame['error']}")
                                    )
                                    return
                                continue
                            if frame.get("type") == 6:  # ping
                                needs_pong.set()
                                continue
                            if not handle(frame):
                                return
                finally:
                    sender_task.cancel()
        except Exception as exc:  # network failure, DNS, TLS, auth, ...
            if "401" in str(exc) or "403" in str(exc):
                queue.put_nowait(AuthRequired(f"Copilot rejected the token: {exc}"))
            else:
                queue.put_nowait(ProviderUnavailable(f"Could not reach Copilot: {exc}"))
        finally:
            queue.put_nowait(_TurnResult(answer=answer, disengaged=disengaged))

    pump_task = asyncio.create_task(pump())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                pump_task.cancel()
                raise ProviderTimeout(f"Copilot did not answer within {timeout:.0f}s")
            if isinstance(item, _TurnResult):
                if not item.answer and item.disengaged:
                    raise DisengagedError(
                        "Copilot refused the prompt (safety filter). "
                        "Try a shorter, more direct prompt."
                    )
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        pump_task.cancel()
