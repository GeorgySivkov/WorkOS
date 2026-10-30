"""WBXML-кодек для Exchange ActiveSync (MS-ASWBXML, шаг 2.4).

Дерево — маленький класс Node: tag в виде "Пространство:Имя" (например
"AirSync:Sync"), value (str для строк, bytes для opaque) и children.
Атрибутов в EAS нет, строковой таблицы клиент не шлёт.

    tree = el("FolderHierarchy:FolderSync", el("FolderHierarchy:SyncKey", "0"))
    data = encode(tree)
    back = decode(data)

Таблицы токенов сверены с MS-ASWBXML: AirSync(0), Email(2), Calendar(4),
FolderHierarchy(7), Ping(13), Provision(14), AirSyncBase(17), Settings(18),
Email2(22). Незнакомый тег не роняет разбор: он получает имя вида
"Contacts:0x1E" и в таком же виде кодируется обратно.
"""
from __future__ import annotations

from typing import Iterable, Iterator

# ---------- глобальные токены WBXML 1.3 ----------
SWITCH_PAGE = 0x00
END = 0x01
ENTITY = 0x02
STR_I = 0x03
LITERAL = 0x04
STR_T = 0x83
OPAQUE = 0xC3
HAS_CONTENT = 0x40
HAS_ATTRS = 0x80

HEADER = bytes([0x03, 0x01, 0x6A, 0x00])   # WBXML 1.3, неизвестный public id, UTF-8, пустая таблица строк

# ---------- кодовые страницы ----------
PAGE_NAMES = {
    0: "AirSync", 1: "Contacts", 2: "Email", 3: "AirNotify", 4: "Calendar", 5: "Move",
    6: "ItemEstimate", 7: "FolderHierarchy", 8: "MeetingResponse", 9: "Tasks",
    10: "ResolveRecipients", 11: "ValidateCert", 12: "Contacts2", 13: "Ping", 14: "Provision",
    15: "Search", 16: "GAL", 17: "AirSyncBase", 18: "Settings", 19: "DocumentLibrary",
    20: "ItemOperations", 21: "ComposeMail", 22: "Email2", 23: "Notes", 24: "RightsManagement",
    25: "Find",
}


def _seq(start: int, names: str) -> dict[int, str]:
    """Подряд идущие токены: _seq(0x05, "A B C") -> {0x05: A, 0x06: B, 0x07: C}. '-' = дырка."""
    out = {}
    for i, n in enumerate(names.split()):
        if n != "-":
            out[start + i] = n
    return out


TOKENS: dict[int, dict[int, str]] = {
    # Code Page 0: AirSync
    0: _seq(0x05, """Sync Responses Add Change Delete Fetch SyncKey ClientId ServerId Status
                     Collection Class Version CollectionId GetChanges MoreAvailable WindowSize
                     Commands Options FilterType Truncation RTFTruncation Conflict Collections
                     ApplicationData DeletesAsMoves NotifyGUID Supported SoftDelete MIMESupport
                     MIMETruncation Wait Limit Partial ConversationMode MaxItems HeartbeatInterval"""),
    # Code Page 2: Email
    2: _seq(0x05, """Attachment Attachments AttName AttSize Att0Id AttMethod AttRemoved Body BodySize
                     BodyTruncated DateReceived DisplayName DisplayTo Importance MessageClass Subject
                     Read To Cc From ReplyTo AllDayEvent Categories Category DTStamp EndTime
                     InstanceType BusyStatus Location MeetingRequest Organizer RecurrenceId Reminder
                     ResponseRequested Recurrences Recurrence Recurrence_Type Recurrence_Until
                     Recurrence_Occurrences Recurrence_Interval Recurrence_DayOfWeek
                     Recurrence_DayOfMonth Recurrence_WeekOfMonth Recurrence_MonthOfYear StartTime
                     Sensitivity TimeZone GlobalObjId ThreadTopic MIMEData MIMETruncated MIMESize
                     InternetCPID Flag FlagStatus ContentClass FlagType CompleteTime
                     DisallowNewTimeProposal"""),
    # Code Page 4: Calendar
    4: _seq(0x05, """TimeZone AllDayEvent Attendees Attendee Email Name Body BodyTruncated BusyStatus
                     Categories Category CompressedRTF DtStamp EndTime Exception Exceptions Deleted
                     ExceptionStartTime Location MeetingStatus OrganizerEmail OrganizerName
                     Recurrence Type Until Occurrences Interval DayOfWeek DayOfMonth WeekOfMonth
                     MonthOfYear Reminder Sensitivity Subject StartTime UID AttendeeStatus
                     AttendeeType Attachment Attachments AttName AttSize AttOid AttMethod AttRemoved
                     DisplayName DisallowNewTimeProposal ResponseRequested AppointmentReplyTime
                     ResponseType CalendarType IsLeapMonth FirstDayOfWeek OnlineMeetingConfLink
                     OnlineMeetingExternalLink ClientUid"""),
    # Code Page 7: FolderHierarchy
    7: _seq(0x05, """Folders Folder DisplayName ServerId ParentId Type Response Status ContentClass
                     Changes Add Delete Update SyncKey FolderCreate FolderDelete FolderUpdate
                     FolderSync Count Version"""),
    # Code Page 13: Ping
    13: _seq(0x05, "Ping AutdState Status HeartbeatInterval Folders Folder Id Class MaxFolders"),
    # Code Page 14: Provision (политики из EASProvisionDoc нужны только для чтения)
    14: _seq(0x05, """Provision Policies Policy PolicyType PolicyKey Data Status RemoteWipe
                      EASProvisionDoc DevicePasswordEnabled AlphanumericDevicePasswordRequired
                      RequireStorageCardEncryption PasswordRecoveryEnabled - AttachmentsEnabled
                      MinDevicePasswordLength MaxInactivityTimeDeviceLock
                      MaxDevicePasswordFailedAttempts MaxAttachmentSize AllowSimpleDevicePassword
                      DevicePasswordExpiration DevicePasswordHistory AllowStorageCard AllowCamera
                      RequireDeviceEncryption AllowUnsignedApplications
                      AllowUnsignedInstallationPackages MinDevicePasswordComplexCharacters AllowWiFi
                      AllowTextMessaging AllowPOPIMAPEmail AllowBluetooth AllowIrDA
                      RequireManualSyncWhenRoaming AllowDesktopSync MaxCalendarAgeFilter
                      AllowHTMLEmail MaxEmailAgeFilter MaxEmailBodyTruncationSize
                      MaxEmailHTMLBodyTruncationSize RequireSignedSMIMEMessages
                      RequireEncryptedSMIMEMessages RequireSignedSMIMEAlgorithm
                      RequireEncryptionSMIMEAlgorithm AllowSMIMEEncryptionAlgorithmNegotiation
                      AllowSMIMESoftCerts AllowBrowser AllowConsumerEmail AllowRemoteDesktop
                      AllowInternetSharing UnapprovedInROMApplicationList ApplicationName
                      ApprovedApplicationList Hash AccountOnlyRemoteWipe"""),
    # Code Page 17: AirSyncBase (0x09 не используется)
    17: _seq(0x05, """BodyPreference Type TruncationSize AllOrNone - Body Data EstimatedDataSize
                      Truncated Attachments Attachment DisplayName FileReference Method ContentId
                      ContentLocation IsInline NativeBodyType ContentType Preview BodyPartPreference
                      BodyPart Status Add Delete ClientId Content Location Annotation Street City
                      State Country PostalCode Latitude Longitude Accuracy Altitude AltitudeAccuracy
                      LocationUri InstanceId"""),
    # Code Page 18: Settings (0x2A не используется)
    18: _seq(0x05, """Settings Status Get Set Oof OofState StartTime EndTime OofMessage
                      AppliesToInternal AppliesToExternalKnown AppliesToExternalUnknown Enabled
                      ReplyMessage BodyType DevicePassword Password DeviceInformation Model IMEI
                      FriendlyName OS OSLanguage PhoneNumber UserInformation EmailAddresses
                      SmtpAddress UserAgent EnableOutboundSMS MobileOperator PrimarySmtpAddress
                      Accounts Account AccountId AccountName UserDisplayName SendDisabled -
                      RightsManagementInformation"""),
    # Code Page 22: Email2
    22: _seq(0x05, """UmCallerID UmUserNotes UmAttDuration UmAttOrder ConversationId ConversationIndex
                      LastVerbExecuted LastVerbExecutionTime ReceivedAsBcc Sender CalendarType
                      IsLeapMonth AccountId FirstDayOfWeek MeetingMessageType - IsDraft Bcc Send"""),
}

PAGE_BY_NAME = {v: k for k, v in PAGE_NAMES.items()}
_REVERSE: dict[str, tuple[int, int]] = {}
for _page, _toks in TOKENS.items():
    for _tok, _name in _toks.items():
        _REVERSE[f"{PAGE_NAMES[_page]}:{_name}"] = (_page, _tok)


class WBXMLError(ValueError):
    pass


class Node:
    """Элемент дерева EAS. value: str (строка), bytes (opaque) или None."""

    __slots__ = ("tag", "value", "children")

    def __init__(self, tag: str, value: str | bytes | None = None, children: list["Node"] | None = None):
        self.tag = tag
        self.value = value
        self.children = children if children is not None else []

    # ---- навигация ----
    @property
    def name(self) -> str:
        """Имя без пространства: "AirSync:Status" -> "Status"."""
        return self.tag.split(":", 1)[-1]

    def _match(self, child: "Node", step: str) -> bool:
        return child.tag == step if ":" in step else child.name == step

    def find(self, path: str) -> "Node | None":
        """Первый потомок по пути "Collections/Collection/SyncKey" (шаги с пространством или без)."""
        cur: Node | None = self
        for step in path.split("/"):
            if cur is None:
                return None
            cur = next((c for c in cur.children if self._match(c, step)), None)
        return cur

    def findall(self, path: str) -> list["Node"]:
        steps = path.split("/")
        level = [self]
        for step in steps:
            nxt = []
            for n in level:
                nxt.extend(c for c in n.children if self._match(c, step))
            level = nxt
        return level

    def text(self, path: str | None = None, default: str | None = None) -> str | None:
        n = self.find(path) if path else self
        if n is None or n.value is None:
            return default
        if isinstance(n.value, bytes):
            return n.value.decode("utf-8", errors="replace")
        return n.value

    def int(self, path: str | None = None, default: int | None = None) -> int | None:
        t = self.text(path)
        try:
            return int(t) if t is not None and t.strip() != "" else default
        except ValueError:
            return default

    def has(self, path: str) -> bool:
        return self.find(path) is not None

    def iter(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.iter()

    # ---- сравнение и вывод ----
    def __eq__(self, other) -> bool:
        return (isinstance(other, Node) and self.tag == other.tag and self.value == other.value
                and self.children == other.children)

    def __repr__(self) -> str:
        return f"Node({self.tag!r}, {self.value!r}, {len(self.children)} children)"

    def to_xml(self, indent: int = 0) -> str:
        """Человекочитаемый вид для --probe и отладки (не настоящий XML с namespace)."""
        pad = "  " * indent
        if self.children:
            inner = "\n".join(c.to_xml(indent + 1) for c in self.children)
            return f"{pad}<{self.tag}>\n{inner}\n{pad}</{self.tag}>"
        if self.value is None:
            return f"{pad}<{self.tag}/>"
        v = f"[opaque {len(self.value)} байт]" if isinstance(self.value, bytes) else self.value
        return f"{pad}<{self.tag}>{v}</{self.tag}>"


def el(tag: str, *items) -> Node:
    """Удобный конструктор: el(tag, "строка") | el(tag, b"opaque") | el(tag, child, child, ...).

    Числа превращаются в строки, None среди детей пропускается.
    """
    if len(items) == 1 and isinstance(items[0], (str, bytes, int)) and not isinstance(items[0], bool):
        v = items[0]
        return Node(tag, str(v) if isinstance(v, int) else v)
    kids: list[Node] = []
    for it in items:
        if it is None:
            continue
        if isinstance(it, Node):
            kids.append(it)
        elif isinstance(it, Iterable) and not isinstance(it, (str, bytes)):
            kids.extend(x for x in it if x is not None)
        else:
            raise TypeError(f"el({tag}): неожиданный элемент {it!r}")
    return Node(tag, None, kids)


# ---------- кодирование ----------
def _mb_u_int32(n: int) -> bytes:
    if n < 0:
        raise WBXMLError("отрицательное число в mb_u_int32")
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _resolve(tag: str) -> tuple[int, int]:
    hit = _REVERSE.get(tag)
    if hit:
        return hit
    ns, _, name = tag.partition(":")
    if ns in PAGE_BY_NAME and name.lower().startswith("0x"):
        tok = int(name, 16)
        if 0x05 <= tok <= 0x3F:
            return PAGE_BY_NAME[ns], tok
    raise WBXMLError(f"неизвестный тег {tag!r}")


def encode(root: Node) -> bytes:
    out = bytearray(HEADER)
    page = [0]

    def emit(n: Node) -> None:
        p, tok = _resolve(n.tag)
        if p != page[0]:
            out.extend((SWITCH_PAGE, p))
            page[0] = p
        has = n.value is not None or bool(n.children)
        out.append(tok | (HAS_CONTENT if has else 0))
        if not has:
            return
        if n.value is not None:
            if isinstance(n.value, bytes):
                out.append(OPAQUE)
                out.extend(_mb_u_int32(len(n.value)))
                out.extend(n.value)
            else:
                data = str(n.value).encode("utf-8")
                if b"\x00" in data:
                    raise WBXMLError(f"нулевой байт в строке тега {n.tag}")
                out.append(STR_I)
                out.extend(data)
                out.append(0)
        for c in n.children:
            emit(c)
        out.append(END)

    emit(root)
    return bytes(out)


# ---------- декодирование ----------
class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise WBXMLError("WBXML оборван")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def mb(self) -> int:
        n = 0
        for _ in range(5):
            b = self.byte()
            n = (n << 7) | (b & 0x7F)
            if not b & 0x80:
                return n
        raise WBXMLError("слишком длинный mb_u_int32")

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise WBXMLError("WBXML оборван внутри opaque")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def cstr(self) -> bytes:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise WBXMLError("строка без завершающего нуля")
        chunk = self.data[self.pos:end]
        self.pos = end + 1
        return chunk


def tag_name(page: int, tok: int) -> str:
    ns = PAGE_NAMES.get(page, f"Page{page}")
    name = TOKENS.get(page, {}).get(tok)
    return f"{ns}:{name}" if name else f"{ns}:0x{tok:02X}"


def _append_value(node: Node, v: str | bytes) -> None:
    if node.value is None:
        node.value = v
    elif isinstance(node.value, bytes) or isinstance(v, bytes):
        a = node.value if isinstance(node.value, bytes) else node.value.encode("utf-8")
        b = v if isinstance(v, bytes) else v.encode("utf-8")
        node.value = a + b
    else:
        node.value += v


def decode(data: bytes) -> Node | None:
    """Байты WBXML -> корневой Node. Пустой ответ (Sync без изменений) -> None."""
    if not data:
        return None
    r = _Reader(data)
    r.byte()                       # версия
    pub = r.mb()
    if pub == 0:
        r.mb()                     # public id задан индексом в таблице строк
    charset = r.mb()
    if charset not in (0x6A, 0):
        raise WBXMLError(f"неподдерживаемая кодировка WBXML: {charset}")
    strtbl = r.take(r.mb())

    page = 0
    root: Node | None = None
    stack: list[Node] = []
    while r.pos < len(data):
        b = r.byte()
        if b == SWITCH_PAGE:
            page = r.byte()
        elif b == END:
            if not stack:
                raise WBXMLError("лишний END")
            stack.pop()
        elif b == STR_I:
            if not stack:
                raise WBXMLError("строка вне элемента")
            _append_value(stack[-1], r.cstr().decode("utf-8", errors="replace"))
        elif b == STR_T:
            off = r.mb()
            end = strtbl.find(b"\x00", off)
            s = strtbl[off:end if end >= 0 else len(strtbl)]
            if stack:
                _append_value(stack[-1], s.decode("utf-8", errors="replace"))
        elif b == OPAQUE:
            if not stack:
                raise WBXMLError("opaque вне элемента")
            _append_value(stack[-1], r.take(r.mb()))
        elif b == ENTITY:
            if stack:
                _append_value(stack[-1], chr(r.mb()))
        elif b in (LITERAL, 0x40, 0x41, 0x42, 0x43, 0x44, 0x80, 0x81, 0x82, 0x84, 0xC0, 0xC1, 0xC2, 0xC4):
            raise WBXMLError(f"неподдерживаемый глобальный токен 0x{b:02X}")
        else:
            if b & HAS_ATTRS:
                raise WBXMLError("атрибуты в EAS не используются")
            node = Node(tag_name(page, b & 0x3F))
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            else:
                raise WBXMLError("второй корневой элемент")
            if b & HAS_CONTENT:
                stack.append(node)
    if stack:
        raise WBXMLError("WBXML оборван: не все элементы закрыты")
    return root
