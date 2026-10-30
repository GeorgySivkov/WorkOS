"""Сведение людей к одной сущности (шаг 3.2). Работает под workos_reader, модель не зовёт.

Два прохода по work.events:

а) Детерминированные обличья (confidence 1.0, context_id NULL): почта из писем и участников
   календаря, id в Slack, id в Trello. Незнакомый адрес/id заводит человека, событие добавляет
   ему контекст (person_contexts). Один адрес = один человек во всех контекстах:
   Ян из Инфатики, написавший на личную почту, остаётся одной строкой в people.

б) Имена в транскриптах Plaud (kind 'meeting') разрешаются ТОЛЬКО среди участников связанной
   календарной встречи (meta.calendar_event_id -> её meta.attendees -> люди по почте) плюс уже
   записанных meta.participant_ids. Глобального поиска по именам здесь нет как операции.
   Правила: побеждает самая длинная форма («Саша Петров» бьёт «Саша»); голое имя с двумя
   кандидатами не регистрируется, а уходит в identity_candidates; один кандидат -> обличье
   display_name в контексте встречи (confidence 0.8) и participant_ids; имя без кандидатов ->
   кандидат с пустыми подсказками. Голое имя, которое в контексте носят двое, как обличье не
   пишется никогда, даже если на встрече был только один из них.

в) По каждому открытому кандидату одна карточка в outbox (тема inbox) с кнопками
   idc:<cand>:<person> / idc:<cand>:new / idc:<cand>:skip; отметка в work.notices 'idc:<id>'.

Какие ключи meta читаются (коллекторам стоит писать так):
  почта и календарь: from, to, cc, bcc, attendees, organizer. Значение: строка
    «Имя <a@b>» (можно через запятую), {email|address, name} или список таких.
  Slack (источник kind='slack'): user | user_id | slack_user (id или {id, name, real_name, email}),
    user_name / user_real_name, user_email (если есть, Slack-обличье цепляется к человеку по почте).
  Trello (источник kind='trello'): members [{id, fullName|name, username}], idMembers [id].
  Plaud: calendar_event_id (лучше work.events.id календарного события; uid/external_id тоже
    понимаются), participant_ids (дополняются, не затираются).
Что уже обработано, помечается в meta.people_sig (хеш входов): событие с изменившимися
участниками или дозревшим транскриптом перечитывается само.

Окружение: WORKOS_SELF_EMAILS (адреса George через запятую, их людьми не заводим),
WORKOS_SELF_HANDLES (его id в Slack/Trello), WORKOS_SELF_NAMES (как его зовут в транскриптах).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.utils import getaddresses

import psycopg

from core import config, db, tg

log = logging.getLogger("workos.people")

NAME_CONFIDENCE = 0.8
CHUNK = 300

# ---------------------------------------------------------------------------
# Нормализация имён: кириллица и латиница сводятся к одному ключу,
# падежные окончания срезаются («Сашей» = «Саша» = «Sasha», «Петровым» = «Petrov»)
# ---------------------------------------------------------------------------
_TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
       "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
       "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
       "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
_ENDINGS = sorted(["iyu", "iya", "oyu", "eyu", "ey", "oy", "iy", "yy", "ii", "ya", "yu", "ia",
                   "om", "em", "ym", "im", "a", "e", "i", "o", "u", "y"], key=len, reverse=True)
TOKEN_RX = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:[-'’][A-Za-zА-Яа-яЁё]+)*")
_PATRONYMIC = re.compile(r"(вич|вна|ична|инична)$", re.I)
_SURNAME_LIKE = re.compile(r"(ов|ев|ёв|ин|ын|ск|цк|енко|ко|юк|ук|ян|дзе|швили|их|ых|ич|ман|берг)"
                           r"(а|у|ым|ой|ом|е|ы|ий|ая|ого|ому|им|ую|ым)?$", re.I)


def _norm(word: str) -> str:
    """Транслит и латинская нормализация без среза окончаний."""
    w = "".join(_TR.get(ch, ch) for ch in word.lower())
    w = w.replace("x", "ks").replace("w", "v").replace("q", "k").replace("ph", "f").replace("j", "y")
    w = re.sub(r"[^a-z]", "", w)
    return re.sub(r"(.)\1+", r"\1", w)


norm = _norm      # публичное имя: транслит без среза окончаний


def name_key(word: str) -> str:
    """Ключ слова-имени: транслит + срез до двух падежных окончаний, основа не короче двух букв."""
    w = _norm(word)
    for _ in range(2):
        for e in _ENDINGS:
            if w.endswith(e) and len(w) - len(e) >= 2:
                w = w[: -len(e)]
                break
        else:
            break
    return w


def tokens(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in TOKEN_RX.finditer(text or "")]


def _is_cyr(s: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", s))


# Полные имена | уменьшительные. Полное имя человека раскрывается в свою группу;
# уменьшительное («Саша») раскрывается во все группы, где оно есть.
_GROUPS_RAW = [
    "Александр Alexander|Саша Шура Sasha", "Александра Alexandra|Саша Шура Sasha",
    "Алексей Alexey Alexei|Лёша Алёша Lesha", "Анастасия Anastasia|Настя Nastya", "Анатолий|Толя",
    "Андрей Andrew|Андрюша", "Анна Anna Ann|Аня", "Антон|Тоша", "Артём Artem|Тёма", "Арсений|Сеня",
    "Борис|Боря", "Вадим|Вадик", "Валентина|Валя", "Валерий|Валера", "Василий|Вася", "Вера|",
    "Вероника|Ника", "Виктор|Витя", "Виктория|Вика", "Владимир Vladimir|Вова Володя",
    "Владислав|Влад", "Вячеслав|Слава", "Галина|Галя", "Георгий George|Жора Гоша", "Глеб|",
    "Григорий|Гриша", "Даниил Данила Daniel|Даня", "Дарья Daria|Даша", "Денис|", "Диана|",
    "Дмитрий Dmitry|Дима", "Евгений Evgeny Eugene|Женя", "Евгения|Женя", "Егор|",
    "Екатерина Ekaterina Katerina|Катя", "Елена Elena|Лена", "Елизавета|Лиза", "Захар|",
    "Иван Ivan|Ваня", "Игорь|", "Илья Ilya|Илюша", "Инна|", "Ирина Irina|Ира", "Карина|",
    "Кирилл|", "Константин|Костя", "Кристина|", "Ксения Kseniya|Ксюша", "Лариса|", "Лев|Лёва",
    "Леонид|Лёня", "Любовь|Люба", "Людмила|Люда", "Максим Maxim|Макс", "Маргарита|Рита",
    "Марина|", "Мария Maria|Маша Masha", "Марк|", "Матвей|", "Михаил Mikhail Michael|Миша",
    "Надежда|Надя", "Наталья Наталия Natalia|Наташа", "Никита|", "Николай Nikolay|Коля", "Нина|",
    "Оксана|", "Олег|", "Ольга Olga|Оля", "Павел Pavel|Паша", "Пётр Petr|Петя", "Полина|Поля",
    "Роман|Рома", "Руслан|", "Светлана|Света", "Семён|Сеня", "Сергей Sergey|Серёжа Сережа",
    "Софья София Sofia|Соня", "Станислав|Стас", "Степан|Стёпа", "Татьяна Tatiana|Таня",
    "Тимофей|Тима", "Тимур|", "Ульяна|", "Фёдор|Федя", "Филипп|", "Эдуард|Эдик",
    "Юлия Yulia Julia|Юля", "Юрий Yuri|Юра", "Яков|Яша", "Ян Yan|", "Яна|", "Ярослав|Слава",
]


@dataclass
class _Group:
    keys: set[str]
    variants: dict[str, set[str]]       # ключ -> написания


def _build_groups() -> list[_Group]:
    out = []
    for line in _GROUPS_RAW:
        variants: dict[str, set[str]] = {}
        for w in line.replace("|", " ").split():
            variants.setdefault(name_key(w), set()).add(w)
        out.append(_Group(set(variants), variants))
    return out


def _case_forms(name: str) -> set[str]:
    """Падежные формы имени из словаря (строчными, ё -> е): «маша, маши, маше, машу, машей»."""
    w = name.lower().replace("ё", "е")
    if not _is_cyr(w):
        return {w}
    for end, forms in (("ия", ("ия", "ии", "ию", "ией")), ("ья", ("ья", "ьи", "ье", "ью", "ьей")),
                       ("а", ("а", "ы", "и", "е", "у", "ой", "ей", "ою")), ("я", ("я", "и", "е", "ю", "ей", "ею")),
                       ("й", ("й", "я", "ю", "ем", "е")), ("ь", ("ь", "я", "ю", "ем", "е", "и", "ью"))):
        if w.endswith(end):
            return {w[: -len(end)] + f for f in forms}
    return {w + f for f in ("", "а", "у", "ом", "е", "ем", "ым")}


GROUPS = _build_groups()
DICT_VARIANTS: dict[str, set[str]] = {}     # ключ имени из словаря -> написания
NAME_FORMS: set[str] = set()                # все падежные формы имён словаря
for _g in GROUPS:
    for _k, _vs in _g.variants.items():
        DICT_VARIANTS.setdefault(_k, set()).update(_vs)
        for _v in _vs:
            NAME_FORMS |= _case_forms(_v)
# формы имён, которые совпадают с обычными словами: «тема», «поля», «света», «любой»...
STOP_WORDS = {"тема", "темы", "теме", "тему", "темой", "поля", "поле", "полю", "полей", "света", "свете",
              "свету", "слава", "славы", "славе", "славу", "вера", "веры", "вере", "веру", "верой",
              "надежда", "надежды", "надежде", "надежду", "любовь", "любви", "люба", "лев", "льва",
              "роман", "романа", "роману", "романе", "романом", "марка", "марку", "марке",
              "ника", "лиза", "соня", "дани"}


def looks_like_name(word: str) -> bool:
    """Слово похоже на имя из словаря, а не на обычное слово (для строчных и незнакомых имён)."""
    w = word.lower().replace("ё", "е")
    return w in NAME_FORMS and w not in STOP_WORDS


def dict_nominal(word: str) -> str:
    """Именительный для имени из словаря: «Машей» -> «Маша», «Яна» остаётся «Яна»."""
    vs = DICT_VARIANTS.get(name_key(word), set())
    if any(_norm(v) == _norm(word) for v in vs):
        return word[:1].upper() + word[1:]
    return sorted(vs, key=lambda v: (not _is_cyr(v), len(v), v))[0] if vs else word


def self_keys() -> set[str]:
    raw = config.env("WORKOS_SELF_NAMES", "Георгий,Жора,Гоша,George") or ""
    return {name_key(w) for w in raw.split(",") if w.strip()}


def _alternatives(tok: str) -> dict[str, set[str]]:
    """Ключ -> написания: сам токен плюс его группы имён (полные и уменьшительные)."""
    k = name_key(tok)
    out: dict[str, set[str]] = {k: {tok}}
    for g in GROUPS:
        if k in g.keys:
            for kk, vs in g.variants.items():
                out.setdefault(kk, set()).update(vs)
    return out


def name_forms(name: str) -> dict[tuple[str, ...], list[set[str]]]:
    """Формы имени: одиночные токены и пары в обоих порядках, с уменьшительными.

    {('sash', 'petrov'): [{'Саша', 'Sasha', ...}, {'Петров'}], ('petrov',): [{'Петров'}], ...}
    Отчество («Сергеевич», «Петровна») отбрасывается: «Иванов Ян Сергеевич» даёт «Ян Иванов».
    """
    toks = [t for _, _, t in tokens(name) if len(t) >= 2 and not _PATRONYMIC.search(t)]
    out: dict[tuple[str, ...], list[set[str]]] = {}
    alts = [_alternatives(t) for t in toks[:2]]
    for a in alts:
        for ka, va in a.items():
            if ka:
                out.setdefault((ka,), [set()])[0].update(va)
    if len(alts) == 2:
        for ka, va in alts[0].items():
            for kb, vb in alts[1].items():
                if not ka or not kb:
                    continue
                for key, (x, y) in (((ka, kb), (va, vb)), ((kb, ka), (vb, va))):
                    cur = out.setdefault(key, [set(), set()])
                    cur[0].update(x)
                    cur[1].update(y)
    return out


@dataclass
class Mention:
    raw: str                       # как назвать в карточке и в identities (именительный, если удалось)
    surface: str                   # как сказано в тексте
    key: tuple[str, ...]
    pids: set[int] = field(default_factory=set)
    start: int = 0


class NameIndex:
    """Формы имён набора людей: участников встречи, людей контекста или всех (retrieve)."""

    def __init__(self) -> None:
        self.forms: dict[tuple[str, ...], set[int]] = {}
        self.variants: dict[tuple[tuple[str, ...], int], list[set[str]]] = {}
        self.names: dict[int, str] = {}

    def add(self, pid: int, display_name: str, extra: list[str] | tuple = ()) -> None:
        self.names.setdefault(pid, display_name)
        for name in [display_name, *[e for e in extra if e]]:
            for key, vs in name_forms(name).items():
                self.forms.setdefault(key, set()).add(pid)
                cur = self.variants.setdefault((key, pid), [set() for _ in key])
                for i, v in enumerate(vs):
                    cur[i].update(v)

    def _nominal(self, surface_toks: list[str], key: tuple[str, ...], pids: set[int]) -> str:
        parts = []
        for i, tok in enumerate(surface_toks):
            vs: set[str] = set()
            for p in pids:
                vs |= self.variants.get((key, p), [set()] * len(key))[i]
            if not vs or any(_norm(v) == _norm(tok) for v in vs):
                parts.append(tok[:1].upper() + tok[1:])     # сказано в именительном: берём как сказано
            else:
                parts.append(sorted(vs, key=lambda v: (not _is_cyr(v), len(v), v))[0])
        return " ".join(parts)

    def scan(self, text: str, require_cap: bool = True, skip_keys: set[str] | None = None) -> list[Mention]:
        """Упоминания людей в тексте: сначала пары токенов, потом одиночные (длинная форма бьёт короткую).

        require_cap: в транскрипте имя пишется с заглавной; в вопросе George может писать строчными,
        тогда одиночное слово строчными считается именем, только если оно есть в словаре имён.
        Имена из словаря без кандидатов возвращаются с пустым pids («не знаю такого»).
        """
        skip_keys = skip_keys or set()
        toks = [(s, e, w, name_key(w)) for s, e, w in tokens(text)]
        out: list[Mention] = []
        i, n = 0, len(toks)

        def adjacent(j: int) -> bool:
            return j + 1 < n and text[toks[j][1]:toks[j + 1][0]].strip() == ""

        def cap(w: str) -> bool:
            return w[:1].isupper()

        while i < n:
            s, e, w, k = toks[i]
            if len(w) < 2 or not k or (require_cap and not cap(w)):
                i += 1
                continue
            nxt = toks[i + 1] if adjacent(i) else None
            if nxt and (k, nxt[3]) in self.forms and (not require_cap or cap(nxt[2])):
                key = (k, nxt[3])
                pids = self.forms[key]
                out.append(Mention(self._nominal([w, nxt[2]], key, pids), f"{w} {nxt[2]}", key, set(pids), s))
                i += 2
                continue
            if k in skip_keys:
                i += 1
                continue
            namelike = looks_like_name(w)
            surname_next = bool(nxt and cap(nxt[2]) and _SURNAME_LIKE.search(nxt[2])
                                and (nxt[3],) not in self.forms)
            known = (k,) in self.forms and (cap(w) or namelike)
            if (known or namelike) and surname_next:
                # «Саша Смирнов», а Смирнова среди людей нет: это другой Саша, не наш
                raw = f"{dict_nominal(w) if namelike else w} {nxt[2]}"
                out.append(Mention(raw, f"{w} {nxt[2]}", (k, nxt[3]), set(), s))
                i += 2
                continue
            if known:
                pids = self.forms[(k,)]
                out.append(Mention(self._nominal([w], (k,), pids), w, (k,), set(pids), s))
            elif namelike and (cap(w) or not require_cap):
                out.append(Mention(dict_nominal(w), w, (k,), set(), s))    # имя есть, человека нет
            i += 1
        return out


def unique_mentions(mentions: list[Mention]) -> list[Mention]:
    seen: dict[tuple, Mention] = {}
    for m in mentions:
        seen.setdefault(m.key, m)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Детерминированные обличья
# ---------------------------------------------------------------------------
_SKIP_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|noreply|mailer-daemon|postmaster|notifications?|"
                         r"bounces?|calendar-notification|invitations?)([+._-].*)?$", re.I)
_SKIP_DOMAIN = re.compile(r"(resource\.calendar\.google\.com|group\.calendar\.google\.com|"
                          r"calendar\.google\.com)$", re.I)
EMAIL_KEYS = ("from", "to", "cc", "bcc", "attendees", "organizer")
SLACK_KEYS = ("user", "user_id", "slack_user", "user_name", "user_real_name", "user_email")
TRELLO_KEYS = ("members", "idMembers")
SIG_KEYS = (*EMAIL_KEYS, *SLACK_KEYS, *TRELLO_KEYS, "participant_ids", "calendar_event_id")


def _env_set(name: str) -> set[str]:
    return {x.strip().lower() for x in (config.env(name, "") or "").split(",") if x.strip()}


def addresses(value) -> list[tuple[str, str]]:
    """(имя, адрес) из строки «Имя <a@b>, ...», словаря {email, name} или списка таких."""
    out: list[tuple[str, str]] = []
    if not value:
        return out
    if isinstance(value, dict):
        addr = value.get("email") or value.get("address") or value.get("emailAddress") or ""
        if isinstance(addr, dict):          # {"emailAddress": {"address":..., "name":...}}
            return addresses(addr)
        name = value.get("name") or value.get("displayName") or value.get("cn") or ""
        if addr:
            out.append((str(name), str(addr)))
        return out
    if isinstance(value, (list, tuple)):
        for v in value:
            out.extend(addresses(v))
        return out
    for name, addr in getaddresses([str(value)]):
        if addr:
            out.append((name, addr))
    return out


def _clean_name(name: str | None) -> str | None:
    name = re.sub(r"\s+", " ", (name or "").strip().strip("'\"")).strip()
    if not name or "@" in name:
        return None
    if "," in name:                        # «Иванов, Ян» -> «Ян Иванов»
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}".strip()
    return name or None


def fallback_name(addr: str) -> str:
    local = addr.split("@")[0]
    parts = [p for p in re.split(r"[._+-]+", local) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts) or local


class People:
    """Кеш и операции записи над people/identities/person_contexts в рамках одного прогона."""

    def __init__(self, conn):
        self.conn = conn
        self.self_emails = _env_set("WORKOS_SELF_EMAILS")
        self.self_handles = _env_set("WORKOS_SELF_HANDLES")
        self.cache: dict[tuple[str, str], int] = {}
        self.created = 0
        self.linked = 0

    def _find(self, kind: str, value: str) -> int | None:
        if (kind, value) in self.cache:
            return self.cache[(kind, value)]
        row = self.conn.execute("SELECT person_id FROM work.identities WHERE kind = %s AND value = %s "
                                "AND context_id IS NULL", (kind, value)).fetchone()
        if row:
            self.cache[(kind, value)] = row["person_id"]
            return row["person_id"]
        return None

    def _create(self, kind: str, value: str, display: str) -> int:
        pid = self.conn.execute("INSERT INTO work.people (display_name) VALUES (%s) RETURNING id",
                                (display,)).fetchone()["id"]
        got = self.conn.execute(
            "INSERT INTO work.identities (person_id, kind, value, context_id, confidence) "
            "VALUES (%s, %s, %s, NULL, 1.0) ON CONFLICT DO NOTHING RETURNING person_id",
            (pid, kind, value)).fetchone()
        if not got:                         # гонка: обличье уже кто-то записал
            self.conn.execute("DELETE FROM work.people WHERE id = %s", (pid,))
            return self._find(kind, value)
        self.created += 1
        self.cache[(kind, value)] = pid
        return pid

    def attach(self, pid: int, kind: str, value: str) -> None:
        r = self.conn.execute(
            "INSERT INTO work.identities (person_id, kind, value, context_id, confidence) "
            "VALUES (%s, %s, %s, NULL, 1.0) ON CONFLICT DO NOTHING RETURNING id", (pid, kind, value)).fetchone()
        if r:
            self.linked += 1
        self.cache[(kind, value)] = self._find(kind, value) or pid

    def in_context(self, pid: int | None, context_id: str | None) -> None:
        if pid and context_id:
            self.conn.execute("INSERT INTO work.person_contexts (person_id, context_id) VALUES (%s, %s) "
                              "ON CONFLICT DO NOTHING", (pid, context_id))

    def email(self, addr: str, name: str | None, context_id: str | None) -> int | None:
        addr = (addr or "").strip().strip("<>").lower()
        if "@" not in addr or " " in addr or addr in self.self_emails:
            return None
        local, _, domain = addr.rpartition("@")
        if not local or not domain or _SKIP_LOCAL.match(local) or _SKIP_DOMAIN.search(domain):
            return None
        name = _clean_name(name)
        pid = self._find("email", addr)
        if pid is None:
            pid = self._create("email", addr, name or fallback_name(addr))
        elif name:
            # имя из заголовка лучше имени, собранного из адреса
            self.conn.execute("UPDATE work.people SET display_name = %s WHERE id = %s "
                              "AND lower(display_name) IN (%s, %s, %s)",
                              (name, pid, addr, fallback_name(addr).lower(), addr.split("@")[0]))
        self.in_context(pid, context_id)
        return pid

    def handle(self, kind: str, value: str, name: str | None, context_id: str | None,
               email: str | None = None) -> int | None:
        value = (value or "").strip()
        if not value or value.lower() in self.self_handles:
            return None
        pid = self._find(kind, value)
        if pid is None and email:
            pid = self.email(email, name, context_id)
            if pid:
                self.attach(pid, kind, value)
        if pid is None:
            name = _clean_name(name)
            if not name:
                return None               # без имени человека не заводим: только мусор в people
            pid = self._create(kind, value, name)
        self.in_context(pid, context_id)
        return pid


def _slack_users(meta: dict, actor_raw: str | None) -> list[tuple[str, str | None, str | None]]:
    u = meta.get("user") or meta.get("user_id") or meta.get("slack_user")
    if isinstance(u, dict):
        return [(str(u.get("id") or ""), u.get("real_name") or u.get("name"),
                 u.get("email") or (u.get("profile") or {}).get("email"))]
    if isinstance(u, str) and u:
        return [(u, meta.get("user_real_name") or meta.get("user_name") or actor_raw, meta.get("user_email"))]
    return []


def _trello_members(meta: dict) -> list[tuple[str, str | None]]:
    out = []
    for m in meta.get("members") or []:
        if isinstance(m, dict) and m.get("id"):
            out.append((str(m["id"]), m.get("fullName") or m.get("name") or m.get("username")))
        elif isinstance(m, str):
            out.append((m, None))
    for mid in meta.get("idMembers") or []:
        out.append((str(mid), None))
    return out


# ---------------------------------------------------------------------------
# Проход по событиям
# ---------------------------------------------------------------------------
def _sig_sql(alias: str = "e") -> str:
    """Хеш входов, от которых зависит результат: изменились участники или тело встречи -> перечитать."""
    pairs = ", ".join(f"'{k}', {alias}.meta->'{k}'" for k in SIG_KEYS)
    cal = (f"(SELECT c.meta->'attendees' FROM work.events c WHERE c.id = CASE WHEN "
           f"{alias}.meta->>'calendar_event_id' ~ '^[0-9]+$' THEN ({alias}.meta->>'calendar_event_id')::bigint END)")
    return (f"md5(coalesce({alias}.actor_raw, '') || '|' || jsonb_build_object({pairs})::text || '|' || "
            f"CASE WHEN {alias}.kind = 'meeting' THEN coalesce(octet_length({alias}.body), 0)::text || '|' || "
            f"coalesce({cal}::text, '') ELSE '' END)")


PENDING = f"""
SELECT e.id, e.kind, e.context_id, e.title, e.actor_raw, e.actor_person_id, e.meta, s.kind AS source_kind,
       CASE WHEN e.kind = 'meeting' THEN e.body END AS body
FROM work.events e JOIN work.sources s ON s.id = e.source_id
WHERE (e.meta->>'people_sig') IS DISTINCT FROM {_sig_sql('e')}
  AND e.id <> ALL(%(skip)s::bigint[])
ORDER BY (e.kind = 'meeting'), e.occurred_at
LIMIT %(n)s
"""


def _int_ids(values) -> set[int]:
    out = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _write_participants(conn, ev: dict, pids: set[int], actor: int | None) -> None:
    old = _int_ids((ev["meta"] or {}).get("participant_ids"))
    new = sorted(old | pids)
    if new != sorted(old):
        conn.execute("UPDATE work.events SET meta = meta || jsonb_build_object('participant_ids', %s::jsonb) "
                     "WHERE id = %s", (db.jb(new), ev["id"]))
    if actor and not ev.get("actor_person_id"):
        conn.execute("UPDATE work.events SET actor_person_id = %s WHERE id = %s AND actor_person_id IS NULL",
                     (actor, ev["id"]))


def _mark(conn, event_id: int) -> None:
    conn.execute(f"UPDATE work.events e SET meta = e.meta || jsonb_build_object('people_sig', {_sig_sql('e')}) "
                 f"WHERE e.id = %s", (event_id,))


def _process_identities(people: People, ev: dict) -> tuple[set[int], int | None]:
    meta, ctx = ev["meta"] or {}, ev["context_id"]
    pids: set[int] = set()
    actor = None
    for key in EMAIL_KEYS:
        for name, addr in addresses(meta.get(key)):
            pid = people.email(addr, name, ctx)
            if pid:
                pids.add(pid)
                if key == "from" and actor is None:
                    actor = pid
    if actor is None and ev["kind"] == "email" and ev.get("actor_raw"):
        for name, addr in addresses(ev["actor_raw"]):
            actor = people.email(addr, name, ctx) or actor
        if actor:
            pids.add(actor)
    if ev["source_kind"] == "slack":
        for uid, name, email in _slack_users(meta, ev.get("actor_raw")):
            pid = people.handle("slack", uid, name, ctx, email)
            if pid:
                pids.add(pid)
                actor = actor or pid
    if ev["source_kind"] == "trello":
        for mid, name in _trello_members(meta):
            pid = people.handle("trello", mid, name, ctx)
            if pid:
                pids.add(pid)
    return pids, actor


def _calendar_event(conn, ref) -> dict | None:
    if ref in (None, ""):
        return None
    ref = str(ref)
    if ref.isdigit():
        row = conn.execute("SELECT id, context_id, meta FROM work.events WHERE id = %s", (int(ref),)).fetchone()
        if row:
            return row
    return conn.execute("SELECT id, context_id, meta FROM work.events WHERE kind = 'calendar' "
                        "AND (external_id = %s OR meta @> jsonb_build_object('uid', %s::text)) "
                        "ORDER BY occurred_at DESC LIMIT 1", (ref, ref)).fetchone()


def _index_for(conn, pids: set[int], context_id: str) -> NameIndex:
    idx = NameIndex()
    if not pids:
        return idx
    rows = conn.execute(
        """SELECT p.id, p.display_name,
                  coalesce(array_agg(i.value) FILTER (WHERE i.kind = 'display_name'
                           AND (i.context_id IS NULL OR i.context_id = %s)), '{}') AS names
           FROM work.people p LEFT JOIN work.identities i ON i.person_id = p.id
           WHERE p.id = ANY(%s::bigint[]) GROUP BY p.id""", (context_id, sorted(pids))).fetchall()
    for r in rows:
        idx.add(r["id"], r["display_name"], r["names"])
    return idx


def _context_index(conn, context_id: str) -> NameIndex:
    rows = conn.execute("SELECT person_id FROM work.person_contexts WHERE context_id = %s", (context_id,)).fetchall()
    return _index_for(conn, {r["person_id"] for r in rows}, context_id)


context_index = _context_index      # публичное имя: бот проверяет голые имена перед записью обличья


def bare_key(raw: str) -> tuple[str, ...] | None:
    """Ключ голого имени («Саша», «Сашей») или None, если в строке больше одного слова-имени."""
    toks = [t for _, _, t in tokens(raw) if len(t) >= 2 and not _PATRONYMIC.search(t)]
    return (name_key(toks[0]),) if len(toks) == 1 and name_key(toks[0]) else None


def shared_bare_name(conn, raw: str, context_id: str | None, person_id: int | None = None) -> bool:
    """Голое имя, которое в контексте носит кто-то, кроме person_id: как обличье его писать нельзя."""
    key = bare_key(raw)
    if not key or not context_id:
        return False
    return bool(context_index(conn, context_id).forms.get(key, set()) - {person_id})


def _suggestion(conn, pid: int) -> dict:
    r = conn.execute(
        """SELECT p.display_name,
                  coalesce(array_agg(c.id ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL), '{}') AS ctx,
                  coalesce(array_agg(c.title ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL), '{}') AS titles,
                  (SELECT i.value FROM work.identities i WHERE i.person_id = p.id AND i.kind = 'email'
                   ORDER BY i.id LIMIT 1) AS email
           FROM work.people p
           LEFT JOIN work.person_contexts pc ON pc.person_id = p.id
           LEFT JOIN work.contexts c ON c.id = pc.context_id
           WHERE p.id = %s GROUP BY p.id""", (pid,)).fetchone()
    return {"person_id": pid, "name": r["display_name"], "contexts": list(r["ctx"]),
            "context_titles": list(r["titles"]), "email": r["email"]}


def _add_candidate(conn, ev: dict, m: Mention) -> int | None:
    ctx = ev["context_id"]
    exists = conn.execute("SELECT 1 FROM work.identity_candidates WHERE source_event = %s AND lower(raw) = lower(%s)",
                          (ev["id"], m.raw)).fetchone()
    if exists:
        return None
    if not m.pids:
        # незнакомое имя спрашиваем в контексте один раз: «пропустить» значит не спрашивать снова
        known = conn.execute(
            """SELECT 1 FROM work.identity_candidates WHERE lower(raw) = lower(%(raw)s) AND context_id = %(ctx)s
                 AND suggestions = '[]'::jsonb
               UNION ALL
               SELECT 1 FROM work.identities WHERE kind = 'display_name' AND lower(value) = lower(%(raw)s)
                 AND context_id = %(ctx)s
               LIMIT 1""", {"raw": m.raw, "ctx": ctx}).fetchone()
        if known:
            return None
    sugg = [_suggestion(conn, p) for p in sorted(m.pids)]
    return conn.execute(
        "INSERT INTO work.identity_candidates (raw, kind, source_event, context_id, suggestions) "
        "VALUES (%s, 'display_name', %s, %s, %s) RETURNING id", (m.raw, ev["id"], ctx, db.jb(sugg))).fetchone()["id"]


def _register_name(conn, pid: int, m: Mention, context_id: str, ctx_index: NameIndex) -> bool:
    if len(m.key) == 1 and ctx_index.forms.get(m.key, set()) - {pid}:
        return False                        # голое имя в контексте носят двое: обличьем не пишем никогда
    r = conn.execute(
        "INSERT INTO work.identities (person_id, kind, value, context_id, confidence) "
        "VALUES (%s, 'display_name', %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
        (pid, m.raw, context_id, NAME_CONFIDENCE)).fetchone()
    return bool(r)


def _process_meeting(conn, people: People, ev: dict, stats: dict) -> None:
    meta, ctx = ev["meta"] or {}, ev["context_id"]
    participants = _int_ids(meta.get("participant_ids"))
    cal = _calendar_event(conn, meta.get("calendar_event_id"))
    if cal:
        for name, addr in addresses((cal["meta"] or {}).get("attendees")):
            pid = people.email(addr, name, cal["context_id"])
            if pid:
                participants.add(pid)
    for pid in participants:
        people.in_context(pid, ctx)
    if not ev.get("body") or not (cal or participants):
        # нет транскрипта или не с кем сверять: имена не разрешаем, ждём связки с календарём
        stats["meetings_waiting"] += 1
        _write_participants(conn, ev, participants, None)
        return
    idx = _index_for(conn, participants, ctx)
    ctx_index = _context_index(conn, ctx)
    resolved: set[int] = set()
    for m in unique_mentions(idx.scan(ev["body"], require_cap=True, skip_keys=self_keys())):
        if len(m.pids) == 1:
            pid = next(iter(m.pids))
            resolved.add(pid)
            if _register_name(conn, pid, m, ctx, ctx_index):
                stats["names"] += 1
        elif _add_candidate(conn, ev, m):
            stats["candidates"] += 1
    _write_participants(conn, ev, participants | resolved, None)
    stats["meetings"] += 1


def _process(conn, people: People, ev: dict, stats: dict) -> None:
    if ev["kind"] == "meeting":
        _process_meeting(conn, people, ev, stats)
    else:
        pids, actor = _process_identities(people, ev)
        _write_participants(conn, ev, pids, actor)
    _mark(conn, ev["id"])
    stats["events"] += 1


# ---------------------------------------------------------------------------
# Карточки в бот
# ---------------------------------------------------------------------------
_WHERE = {"meeting": "На встрече", "email": "В письме", "message": "В сообщении", "card": "В карточке",
          "note": "В заметке", "calendar": "Во встрече"}


def card(cand: dict) -> tuple[str, list]:
    """Текст и кнопки карточки «Кто это?» для кандидата (HTML Telegram)."""
    raw = tg.esc(cand["raw"])
    ctx = tg.esc(cand.get("context_title") or cand.get("context_id") or "")
    if cand.get("event_kind"):
        where = f"{_WHERE.get(cand['event_kind'], 'В записи')} «{tg.esc(cand.get('event_title') or 'без названия')}»"
        text = f"{where} ({ctx}) упомянут „{raw}“. Кто это?"
    else:
        text = f"В контексте {ctx} упомянут „{raw}“. Кто это?"
    sugg = cand.get("suggestions") or []
    if not sugg:
        text += "\nСреди участников такого нет."
    labels = []
    for s in sugg:
        label = f"{s.get('name') or '?'}"[:40]
        titles = s.get("context_titles") or s.get("contexts") or []
        if titles:
            label += f" ({', '.join(titles)})"
        labels.append(label)
    rows = []
    for s, label in zip(sugg, labels):
        if labels.count(label) > 1 and s.get("email"):
            label += f" · {s['email']}"
        rows.append([tg.btn(label, f"idc:{cand['id']}:{s['person_id']}")])
    rows.append([tg.btn("Новый человек", f"idc:{cand['id']}:new"), tg.btn("Пропустить", f"idc:{cand['id']}:skip")])
    return text, rows


def send_cards(conn) -> int:
    rows = conn.execute(
        """SELECT c.id, c.raw, c.suggestions, c.context_id, x.title AS context_title,
                  e.kind AS event_kind, e.title AS event_title
           FROM work.identity_candidates c
           LEFT JOIN work.events e ON e.id = c.source_event
           LEFT JOIN work.contexts x ON x.id = c.context_id
           WHERE c.status = 'open'
             AND NOT EXISTS (SELECT 1 FROM work.notices n WHERE n.key = 'idc:' || c.id)
           ORDER BY c.id""").fetchall()
    sent = 0
    for cand in rows:
        text, buttons = card(cand)
        with conn.transaction():
            if conn.execute("INSERT INTO work.notices (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING key",
                            (f"idc:{cand['id']}",)).fetchone():
                conn.execute("INSERT INTO work.outbox (topic, text, buttons) VALUES ('inbox', %s, %s)",
                             (text, db.jb(buttons)))
                sent += 1
    return sent


# ---------------------------------------------------------------------------
def _run(conn, limit: int | None) -> dict:
    people = People(conn)
    stats = {"events": 0, "meetings": 0, "meetings_waiting": 0, "names": 0, "candidates": 0, "errors": 0}
    failed: list[int] = []
    while limit is None or stats["events"] + len(failed) < limit:
        n = CHUNK if limit is None else min(CHUNK, limit - stats["events"] - len(failed))
        batch = conn.execute(PENDING, {"skip": failed, "n": n}).fetchall()
        if not batch:
            break
        for ev in batch:
            try:
                with conn.transaction():
                    _process(conn, people, ev, stats)
            except Exception as e:  # noqa: BLE001 — одно кривое событие не держит остальные
                failed.append(ev["id"])
                stats["errors"] += 1
                log.warning("people: событие %s: %s", ev["id"], e)
                people.cache.clear()
    stats["people_created"] = people.created
    stats["cards"] = send_cards(conn)
    return stats


def run(date: str | None = None, dry_run: bool = False, limit: int | None = None, **kw) -> dict:
    with db.connect(autocommit=True) as conn:
        if not dry_run:
            return _run(conn, limit)
        out: dict = {}
        with conn.transaction():
            out = _run(conn, limit)
            out["dry_run"] = True
            raise psycopg.Rollback()
        return out


if __name__ == "__main__":
    print(run())
