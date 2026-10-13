"""
ClassVision AI - Assistant Service (Step 12)
--------------------------------------------
A natural-language layer over the Step 7 analytics functions.

    It answers questions by CALLING analytics_service. It computes no
    attendance figure, applies no threshold, and narrates nothing from raw
    rows.

THERE IS NO MODEL HERE, AND NO NETWORK CALL

    This is a question router, not a language model: an intent table of
    regular expressions over a small, closed vocabulary. It can be read in one
    sitting, it gives the same answer twice, and it works on a machine with no
    internet connection - which is the machine this project is built for.

    That is a deliberate trade and it has a cost, stated plainly rather than
    hidden: a question phrased outside the patterns below is NOT answered.
    There is no paraphrasing and no fuzzy intent guessing. The assistant says
    it did not understand and lists what it can be asked, because the failure
    mode of guessing is a confident answer to a question nobody asked.

WHY IT MAY NOT DO ITS OWN ARITHMETIC

    This is the whole constraint the roadmap set for Step 12, and it is worth
    restating as a rule about trust rather than about code.

    A teacher reading "Asha attended 71% of sessions" must be able to open the
    Students page, click Asha, and see 71%. If the assistant computed its own
    figure it would be a second, unverifiable source of truth about a child's
    attendance - and when the two disagreed, nothing would tell anybody which
    one was wrong.

    So every figure in every answer is taken VERBATIM from an analytics
    result, and every answer carries the function that produced it and where
    in the dashboard to reproduce it. `test_assistant.py` asserts the values
    are identical objects to the analytics ones, not merely equal.

    The one transformation allowed is formatting - turning the ratio 0.7143
    into the string "71.4%" - and it happens in exactly one function,
    `_percent`, over a value that has already been returned by analytics.

WHAT IT WILL NOT DO

    Invent a number. "No data" is an answer, and it is given as "no data"
    rather than as 0%.

    Answer about a student or class the caller may not see. The answer is the
    same one they would get for a student who does not exist, so the assistant
    cannot be used to discover who is in somebody else's class.

    Guess between two students whose names both match. It asks which.

    Predict, advise, or explain WHY attendance is what it is. It reports
    measured figures and the deterministic indicator rule, and says who to
    ask about anything else.
"""

import re

# Where in the dashboard each answer can be checked by hand. An answer a
# teacher cannot reproduce is not a trustworthy answer, so every intent names
# the page that shows the same number.
WHERE = {
    "student": "Students -> open the student",
    "class": "Reports -> By class",
    "session": "Sessions -> open the session",
    "overview": "Overview",
    "attention": "Overview -> Needs attention",
}

ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# The vocabulary this assistant is about. A question that names no student, no
# class and no date, and uses none of these words, is not an attendance
# question - and is answered as not understood rather than as a student nobody
# is called. "What is the weather like?" must not come back as "I have no
# student by that name".
ATTENDANCE_WORDS = re.compile(
    r"\b(attendance|attend(?:ed|ing)?|present|presence|absent|absence|"
    r"absences|late|lateness|miss(?:ed|ing)?|rate|percent|percentage|"
    r"session|sessions|lesson|register|roll|student|students|class|classes|"
    r"trend|improving)\b")

# A question with nothing in it. Asked for explicitly rather than falling
# through to "not understood", because an empty box is a different mistake.
BLANK = "Ask a question about attendance. For example: what can you tell me?"

MAX_QUESTION = 400

# A question is a sentence, not a corpus. Both bounds exist so one request
# cannot turn into dozens of database reads.
MAX_ID_LOOKUPS = 8
MAX_NAME_WORDS = 6

# How many students an indicator answer names before pointing at the page that
# lists them all. A sentence naming forty students is not an answer.
MAX_NAMED = 10


def _percent(value):
    """
    A ratio from analytics as a percentage string, or None.

    The ONLY arithmetic in this module, and it is formatting: the value has
    already been computed by analytics_service and is not changed, only
    rendered. None stays None so the caller can say "no data" rather than
    printing a confident 0%.
    """
    if value is None:
        return None
    return "%.1f%%" % (value * 100)


def _count(value):
    """A count as a word, where 0 is a real answer and None is not."""
    return "no data" if value is None else str(value)


class Answer:
    """
    One answer, and the receipt that lets somebody check it.

    `figures` holds the values exactly as analytics returned them - the same
    objects, not copies that have been rounded on the way past. `sources`
    names the function that produced them and where the dashboard shows the
    same thing.
    """

    __slots__ = ("intent", "text", "figures", "sources", "understood",
                 "suggestions")

    def __init__(self, intent, text, figures=None, sources=None,
                 understood=True, suggestions=None):
        self.intent = intent
        self.text = text
        self.figures = figures or {}
        self.sources = sources or []
        self.understood = understood
        self.suggestions = suggestions or []

    def as_dict(self):
        return {
            "understood": self.understood,
            "intent": self.intent,
            "answer": self.text,
            "figures": self.figures,
            "sources": self.sources,
            "suggestions": self.suggestions,
            "note": ("Every figure above was returned by the analytics "
                     "function named in sources. This assistant computes no "
                     "attendance figure of its own."),
        }


class AssistantService:
    """
    Question in, analytics call out, measured figure back.

    Built on top of AnalyticsService and DataService rather than on the
    database, so it cannot reach a row that those two have not already
    scoped and defined.
    """

    def __init__(self, database):
        from backend.services.analytics_service import AnalyticsService
        from backend.services.data_service import DataService

        self.database = database
        self.analytics = AnalyticsService(database)
        self.data = DataService(database)

    # -- the public surface -------------------------------------------------

    def capabilities(self):
        """
        Everything it can be asked, with the analytics call behind each.

        Published rather than described in prose, because a router that
        answers a closed set of questions owes the user the list. The same
        list is what an unrecognised question is answered with.
        """
        return {
            "items": [
                {
                    "intent": intent,
                    "question": example,
                    "calls": calls,
                    "where": WHERE[where],
                }
                for intent, example, calls, where in INTENT_CATALOGUE
            ],
            "note": ("This assistant answers by calling the analytics "
                     "functions listed here. A question outside this list is "
                     "not answered and not guessed at."),
            "limits": [
                "It computes no attendance figure of its own.",
                "It does not predict, advise, or explain why attendance is "
                "what it is.",
                "It answers only about students and classes your account may "
                "already see.",
                "It is a pattern matcher, not a language model. A question "
                "phrased differently from the examples may not be understood.",
            ],
        }

    def ask(self, question, principal=None):
        """
        Answer one question, or say plainly that it was not understood.

        The order matters: the most specific intents are tried first, so
        "what is Asha's attendance rate in CSE-A" is read as a question about
        Asha rather than about the class.
        """
        text = (question or "").strip()

        if not text:
            return Answer("blank", BLANK, understood=False,
                          suggestions=self._examples())

        if len(text) > MAX_QUESTION:
            return Answer(
                "too_long",
                "That question is longer than this assistant reads. Ask a "
                "shorter one.", understood=False,
                suggestions=self._examples())

        lowered = text.lower()

        for intent, pattern, handler in self._intents():
            match = pattern.search(lowered)
            if match:
                return handler(text, lowered, match, principal)

        return self._not_understood()

    def _not_understood(self):
        return Answer(
            "not_understood",
            "I did not understand that. I answer a fixed set of questions "
            "about attendance that has already been recorded, and I do not "
            "guess at the rest.",
            understood=False, suggestions=self._examples())

    # -- the intent table ---------------------------------------------------

    def _intents(self):
        """
        Most specific first.

        A question naming a student is about that student even when it also
        names their class; a question naming neither is about the whole scope
        the caller can see.
        """
        return [
            ("attention", re.compile(
                r"\b(needs? attention|worth a look|at risk|struggling|"
                r"who should i (?:look at|check|worry about)|"
                r"concern(?:ing|s)?)\b"), self._answer_attention),

            ("student_trend", re.compile(
                r"\b(?:is|are)\s+(?P<who>.+?)\s+"
                r"(improving|getting (?:better|worse)|declining)\b"),
             self._answer_student_trend),

            ("student_lateness", re.compile(
                r"\b(?:how often|how much|how many times)\b.*\blate\b|"
                r"\blate(?:ness)?\s+rate\b|\bis\s+(?P<who>.+?)\s+"
                r"(?:often\s+)?late\b"), self._answer_student_lateness),

            ("student_absences", re.compile(
                r"\b(?:how many|how much)\b.*\b(absent|absences|missed|"
                r"miss)\b"), self._answer_student_absences),

            ("student_rate", re.compile(
                r"\b(?:attendance|present|presence)\b.*\b(?:rate|percent|"
                r"percentage|for|of)\b|\bhow is\b|\bhow has\b|"
                r"\bwhat(?:'s| is)\b"), self._answer_rate),

            ("session", re.compile(
                r"\b(session|lesson|class on|day)\b"), self._answer_session),

            ("overview", re.compile(
                r"\b(overall|in total|everything|all classes|whole school|"
                r"summary|how many students)\b"), self._answer_overview),

            ("help", re.compile(
                r"\b(help|what can you (?:do|tell me|answer)|capabilit)\b"),
             self._answer_help),
        ]

    def _examples(self):
        return [example for _, example, _, _ in INTENT_CATALOGUE]

    def _answer_help(self, text, lowered, match, principal):
        return Answer(
            "help",
            "I answer questions about attendance that has already been "
            "recorded, by calling the same analytics the dashboard uses. "
            "Every answer names the function behind it and where to check it.",
            suggestions=self._examples())

    # -- resolving who and what is being asked about ------------------------

    def _resolve_student(self, text, principal):
        """
        Which student a question is about, or why that cannot be decided.

        Returns (student, problem). Exactly one is not None.

        A student outside the caller's scope is reported as NOT FOUND, which
        is the same answer they would get for a student who does not exist.
        Distinguishing the two would turn this into a way of discovering who
        is in somebody else's class.
        """
        candidates = []
        seen = set()

        # An exact id anywhere in the question wins: it is unambiguous, and an
        # id is what a teacher pastes. Only tokens that could BE an id are
        # looked up - one containing a digit - which keeps a nine-word question
        # from becoming nine primary-key reads. A wholly alphabetic id is
        # still found by the name search below, which matches student_id too.
        tokens = [token for token
                  in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", text)
                  if any(character.isdigit() for character in token)]

        for token in tokens[:MAX_ID_LOOKUPS]:
            record = self.data.get_student(token, with_attendance=False)
            if record and record["student_id"] not in seen:
                candidates.append(record)
                seen.add(record["student_id"])

        if not candidates:
            # A name. Searched through DataService so the same LIKE rule the
            # Students page uses applies here.
            for word in self._name_words(text):
                found = self.data.list_students(
                    search=word, principal=principal, limit=10,
                    with_attendance=False)
                for record in found.get("items", []):
                    if record["student_id"] not in seen:
                        candidates.append(record)
                        seen.add(record["student_id"])

        visible = [record for record in candidates
                   if principal is None or principal.may_see_student(record)]

        if not visible:
            return None, ("I have no student by that name or id. Check the "
                          "spelling, or use their student id.")

        if len(visible) > 1:
            names = ", ".join("%s (%s)" % (record["name"] or "unnamed",
                                           record["student_id"])
                              for record in visible[:5])
            return None, ("More than one student matches: %s. Ask again with "
                          "the student id." % names)

        return visible[0], None

    @staticmethod
    def _name_words(text):
        """
        The words in a question that could be part of a name.

        Everything the vocabulary already uses is dropped, so "how is Asha
        doing" searches for Asha and not for "how" - a search for "how" would
        match a student called Howard and answer about the wrong child.
        """
        stop = {
            "what", "whats", "what's", "is", "are", "was", "were", "the", "a",
            "an", "of", "for", "in", "on", "at", "to", "how", "many", "much",
            "often", "attendance", "rate", "percent", "percentage", "present",
            "presence", "absent", "absence", "absences", "missed", "miss",
            "late", "lateness", "trend", "improving", "getting", "better",
            "worse", "declining", "session", "sessions", "lesson", "class",
            "classes", "student", "students", "overall", "total", "summary",
            "doing", "does", "do", "did", "and", "or", "me", "my", "i",
            "tell", "show", "give", "about", "this", "that", "there", "has",
            "have", "had", "been", "being", "be", "it", "its", "can", "you",
            "please", "times", "time", "day", "days", "term", "terms", "all",
            "any", "some", "which", "who", "whom", "whose", "why", "when",
            "where", "report", "reports", "number", "count", "list",
        }

        words = []

        for word in re.findall(r"[A-Za-z][A-Za-z'.-]*", text):
            # "Asha's" is a question about Asha. Trailing punctuation goes
            # too, so "Asha." and "Asha" are the same question.
            word = re.sub(r"(?:'s|s')$", "", word).strip(".-'")
            if len(word) > 1 and word.lower() not in stop:
                words.append(word)

        # Bounded: a long sentence must not turn into forty searches. The
        # first few content words are where a name is.
        return words[:MAX_NAME_WORDS]

    def _resolve_class(self, text, principal):
        """
        Which class a question is about, matched against classes that exist.

        Nothing is invented: the candidate list is the classes that actually
        have sessions, already narrowed by DataService to the ones this
        account may see.
        """
        classes = self.data.list_classes(principal=principal)
        lowered = text.lower()

        def normalise(value):
            return re.sub(r"[^a-z0-9]", "", (value or "").lower())

        target = normalise(lowered)

        # Longest first, so "CSE-A" is not matched as "CSE" when both exist.
        for item in sorted(classes, key=lambda row: -len(row["class_id"] or "")):
            key = normalise(item["class_id"])
            if key and key in target:
                return item["class_id"]

        return None

    @staticmethod
    def _resolve_date(text):
        match = ISO_DATE.search(text)
        return match.group(1) if match else None

    # -- the answers --------------------------------------------------------

    def _answer_rate(self, text, lowered, match, principal):
        """
        An attendance rate. For a student if one is named, else for a class,
        else for everything the caller can see.
        """
        class_id = self._resolve_class(text, principal)
        student, problem = self._resolve_student(text, principal)

        if student is not None:
            return self._student_rate(student)

        if class_id:
            return self._class_rate(class_id)

        # Nothing resolved. Whether that is "I do not know that student" or "I
        # do not understand the question" depends on whether it was an
        # attendance question at all.
        if not ATTENDANCE_WORDS.search(lowered):
            return self._not_understood()

        # A name WAS offered and did not resolve: say so, rather than silently
        # widening the question to the whole school.
        if problem and self._name_words(text):
            return Answer("student_rate", problem, understood=True)

        return self._answer_overview(text, lowered, match, principal)

    def _student_rate(self, student):
        analytics = self.analytics.student_analytics(student["student_id"])

        if analytics is None:
            return Answer(
                "student_rate",
                "%s has not been expected at any completed session, so there "
                "is no attendance rate for them yet."
                % self._label(student),
                sources=[self._source("student_analytics",
                                      {"student_id": student["student_id"]},
                                      "student")])

        rate = analytics["attendance_rate"]
        sessions = analytics["expected_sessions"]

        if rate is None:
            text = ("%s has no attendance rate: they were expected at %s "
                    "completed session(s) with nothing to divide by. That is "
                    "no data, not 0%%."
                    % (self._label(student), _count(sessions)))
        else:
            text = ("%s attended %s of the %d completed session(s) they were "
                    "expected at - an attendance rate of %s."
                    % (self._label(student), analytics["satisfying_count"],
                       sessions, _percent(rate)))

        return Answer(
            "student_rate", text,
            figures={"attendance_rate": rate,
                     "expected_sessions": sessions,
                     "satisfying_count": analytics["satisfying_count"]},
            sources=[self._source("student_analytics",
                                  {"student_id": student["student_id"]},
                                  "student")])

    def _class_rate(self, class_id):
        analytics = self.analytics.class_analytics(class_id)

        if analytics is None or not analytics["completed_sessions"]:
            return Answer(
                "class_rate",
                "%s has no completed session yet, so it has no attendance "
                "rate. That is no data, not 0%%." % class_id,
                sources=[self._source("class_analytics",
                                      {"class_id": class_id}, "class")])

        rate = analytics["attendance_rate"]

        if rate is None:
            text = ("%s has %d completed session(s) but nothing to divide by, "
                    "so it has no attendance rate."
                    % (class_id, analytics["completed_sessions"]))
        else:
            text = ("%s has an attendance rate of %s across %d completed "
                    "session(s), over %d student(s)."
                    % (class_id, _percent(rate),
                       analytics["completed_sessions"],
                       analytics["student_count"]))

        return Answer(
            "class_rate", text,
            figures={"attendance_rate": rate,
                     "completed_sessions": analytics["completed_sessions"],
                     "student_count": analytics["student_count"],
                     "late_rate": analytics["late_rate"],
                     "absence_rate": analytics["absence_rate"]},
            sources=[self._source("class_analytics", {"class_id": class_id},
                                  "class")])

    def _answer_student_absences(self, text, lowered, match, principal):
        student, problem = self._resolve_student(text, principal)

        if student is None:
            class_id = self._resolve_class(text, principal)
            if class_id:
                return self._class_rate(class_id)
            return Answer("student_absences", problem, understood=True)

        analytics = self.analytics.student_analytics(student["student_id"])

        if analytics is None:
            return Answer(
                "student_absences",
                "%s has not been expected at any completed session."
                % self._label(student),
                sources=[self._source("student_analytics",
                                      {"student_id": student["student_id"]},
                                      "student")])

        absent = analytics["absent_sessions"]
        expected = analytics["expected_sessions"]
        missing = analytics["sessions_without_record"]

        text_out = ("%s was marked absent at %s of %d completed session(s) "
                    "they were expected at."
                    % (self._label(student), _count(absent), expected))

        # Expected and never evaluated is a different fact from absent, and
        # saying so is the whole reason this is reported separately.
        if missing:
            text_out += (" A further %d session(s) they were expected at have "
                         "no record at all - nobody decided those, so "
                         "they are not absences." % missing)

        return Answer(
            "student_absences", text_out,
            figures={"absent_sessions": absent,
                     "expected_sessions": expected,
                     "absence_rate": analytics["absence_rate"],
                     "sessions_without_record": missing},
            sources=[self._source("student_analytics",
                                  {"student_id": student["student_id"]},
                                  "student")])

    def _answer_student_lateness(self, text, lowered, match, principal):
        student, problem = self._resolve_student(text, principal)

        if student is None:
            class_id = self._resolve_class(text, principal)
            if class_id:
                return self._class_rate(class_id)
            return Answer("student_lateness", problem, understood=True)

        analytics = self.analytics.student_analytics(student["student_id"])

        if analytics is None:
            return Answer(
                "student_lateness",
                "%s has not been expected at any completed session."
                % self._label(student),
                sources=[self._source("student_analytics",
                                      {"student_id": student["student_id"]},
                                      "student")])

        rate = analytics["late_rate"]
        late = analytics["counts"].get("LATE", 0)

        if rate is None:
            text_out = ("%s has no late rate: there is nothing to divide by."
                        % self._label(student))
        else:
            text_out = ("%s was late at %d of %d completed session(s) they "
                        "were expected at - %s."
                        % (self._label(student), late,
                           analytics["expected_sessions"], _percent(rate)))

        return Answer(
            "student_lateness", text_out,
            figures={"late_rate": rate, "late_sessions": late,
                     "expected_sessions": analytics["expected_sessions"]},
            sources=[self._source("student_analytics",
                                  {"student_id": student["student_id"]},
                                  "student")])

    def _answer_student_trend(self, text, lowered, match, principal):
        student, problem = self._resolve_student(text, principal)

        if student is None:
            return Answer("student_trend", problem, understood=True)

        analytics = self.analytics.student_analytics(student["student_id"])

        if analytics is None:
            return Answer(
                "student_trend",
                "%s has not been expected at any completed session, so there "
                "is no trend." % self._label(student),
                sources=[self._source("student_analytics",
                                      {"student_id": student["student_id"]},
                                      "student")])

        # Two points are a comparison, not a pattern - and analytics is the
        # one that decides whether there are enough. This refuses to call
        # anything a trend that analytics has not called one.
        if not analytics["has_trend_data"]:
            return Answer(
                "student_trend",
                "There is not enough history to call anything a trend for %s. "
                "Analytics needs more completed sessions before a change "
                "between them means anything."
                % self._label(student),
                figures={"has_trend_data": analytics["has_trend_data"],
                         "expected_sessions": analytics["expected_sessions"]},
                sources=[self._source("student_analytics",
                                      {"student_id": student["student_id"]},
                                      "student")])

        trend = analytics["trend"] or {}
        direction = trend.get("direction") or "unknown"
        earlier = _percent(trend.get("earlier_rate"))
        recent = _percent(trend.get("recent_rate"))

        text_out = ("Analytics reports %s as %s."
                    % (self._label(student), direction.lower().replace("_",
                                                                       " ")))

        # The two rates the direction was derived from, so the word can be
        # checked against the numbers rather than taken on trust. Analytics
        # computed both; neither is recomputed here.
        if earlier and recent:
            text_out += (" The satisfying share of their most recent sessions "
                         "is %s, against %s for the sessions before them."
                         % (recent, earlier))

        if trend.get("note"):
            text_out += " %s" % trend["note"]

        return Answer(
            "student_trend", text_out,
            figures={"trend": trend or None,
                     "attendance_rate": analytics["attendance_rate"]},
            sources=[self._source("student_analytics",
                                  {"student_id": student["student_id"]},
                                  "student")])

    def _answer_attention(self, text, lowered, match, principal):
        class_id = self._resolve_class(text, principal)

        result = self.analytics.attention_indicators(class_id=class_id)
        indicators = result["indicators"]

        if principal is not None and not principal.unrestricted:
            indicators = [row for row in indicators
                          if self._may_see_indicator(row, principal)]

        rule = result["rule"]
        scope = class_id or "every class you can see"

        if not indicators:
            text_out = ("Nobody in %s meets the indicator rule: attendance "
                        "below %s, late in more than %s of sessions, or "
                        "absent in more than %s, across at least %d completed "
                        "session(s)."
                        % (scope, _percent(rule["attendance_rate_below"]),
                           _percent(rule["late_rate_above"]),
                           _percent(rule["absence_rate_above"]),
                           rule["minimum_sessions"]))
        else:
            named = "; ".join(
                "%s (%s): %s" % (row["name"] or row["student_id"],
                                 row["student_id"], ", ".join(row["reasons"]))
                for row in indicators[:MAX_NAMED])
            text_out = ("%d student(s) in %s meet the indicator rule: %s."
                        % (len(indicators), scope, named))

            # The remainder as the length of what was not named, rather than
            # as a subtraction. There is no arithmetic in this module by
            # design, and a count of list items is not a measured figure.
            remaining = len(indicators[MAX_NAMED:])
            if remaining:
                text_out += (" %d more are listed on the Overview page."
                             % remaining)

        # Said every time, because a deterministic threshold presented as a
        # judgement is the easiest thing on this page to misread.
        text_out += (" These are arithmetic thresholds, not predictions, and "
                     "no model is involved - the rule is stated so you "
                     "can disagree with it.")

        return Answer(
            "attention", text_out,
            figures={"count": len(indicators), "rule": rule},
            sources=[self._source("attention_indicators",
                                  {"class_id": class_id}, "attention")])

    def _may_see_indicator(self, row, principal):
        student = self.data.get_student(row["student_id"],
                                       with_attendance=False)
        return student is not None and principal.may_see_student(student)

    def _answer_session(self, text, lowered, match, principal):
        """
        One session, or one day.

        A date is answered with the day's summary rather than with a single
        session, because a class can have more than one lesson on a date and
        picking one of them would answer a question nobody asked.
        """
        date = self._resolve_date(text)
        class_id = self._resolve_class(text, principal)

        if date is None:
            if class_id:
                return self._class_rate(class_id)
            return Answer(
                "session",
                "Which session? Give me a date as 2026-03-02, or ask about a "
                "class.", understood=True, suggestions=self._examples())

        result = self.analytics.overview(class_id=class_id, session_date=date)

        # completed_sessions, NOT has_data. has_data answers "does this
        # database hold any session at all", which is true of an installation
        # that has never run a lesson on the date being asked about - and
        # would report 0 sessions while claiming to have data.
        if not result["completed_sessions"]:
            return Answer(
                "session",
                "No completed session on %s%s. A session that was scheduled "
                "or cancelled has no attendance, and one still running is not "
                "final." % (date, " for %s" % class_id if class_id else ""),
                figures={"has_data": result["has_data"]},
                sources=[self._source("overview",
                                      {"class_id": class_id,
                                       "session_date": date}, "session")])

        rate = result["attendance_rate"]

        return Answer(
            "session",
            "On %s%s there were %d completed session(s) over %d expected "
            "student record(s), with an attendance rate of %s."
            % (date, " for %s" % class_id if class_id else "",
               result["completed_sessions"], result["sample_size"],
               _percent(rate) or "no data"),
            figures={"attendance_rate": rate,
                     "completed_sessions": result["completed_sessions"],
                     "sample_size": result["sample_size"],
                     "counts": result["counts"]},
            sources=[self._source("overview",
                                  {"class_id": class_id,
                                   "session_date": date}, "session")])

    def _answer_overview(self, text, lowered, match, principal):
        class_id = self._resolve_class(text, principal)
        result = self.analytics.overview(class_id=class_id)

        scope = class_id or "every class"

        # Scoped, for the same reason as above: a database with sessions in
        # other classes has no attendance rate for a class with none.
        if not result["completed_sessions"]:
            return Answer(
                "overview",
                "There is no completed session in %s yet, so there is no "
                "attendance rate. That is no data, not 0%%." % scope,
                figures={"has_data": result["has_data"],
                         "total_sessions": result["total_sessions"],
                         "total_students": result["total_students"]},
                sources=[self._source("overview", {"class_id": class_id},
                                      "overview")])

        rate = result["attendance_rate"]

        text_out = ("Across %s there are %d completed session(s) and %d "
                    "student(s), with an attendance rate of %s."
                    % (scope, result["completed_sessions"],
                       result["total_students"], _percent(rate) or "no data"))

        # A dataset with more than one policy version is reported rather than
        # averaged over silently; analytics flags it and this repeats the flag.
        if result["has_multiple_policy_versions"]:
            text_out += (" More than one attendance policy version is present "
                         "in this data, so these figures span rules that are "
                         "not identical.")

        return Answer(
            "overview", text_out,
            figures={"attendance_rate": rate,
                     "completed_sessions": result["completed_sessions"],
                     "total_students": result["total_students"],
                     "sample_size": result["sample_size"],
                     "counts": result["counts"]},
            sources=[self._source("overview", {"class_id": class_id},
                                  "overview")])

    # -- receipts -----------------------------------------------------------

    @staticmethod
    def _source(function, arguments, where):
        """
        The receipt for one figure.

        Names the analytics function, the arguments it was given, and the page
        that shows the same number - so an answer can be checked rather than
        believed.
        """
        return {
            "function": "AnalyticsService.%s" % function,
            "arguments": {key: value for key, value in arguments.items()
                          if value is not None},
            "where": WHERE[where],
        }

    @staticmethod
    def _label(student):
        name = (student.get("name") or "").strip()
        return "%s (%s)" % (name, student["student_id"]) if name \
            else student["student_id"]


# Published by capabilities() and used as the suggestion list for a question
# that was not understood. Kept beside the intent table so the two cannot
# drift apart - a capability nobody can trigger is a lie.
INTENT_CATALOGUE = [
    ("student_rate", "What is Asha's attendance rate?",
     ["AnalyticsService.student_analytics"], "student"),
    ("student_absences", "How many sessions has S-1 missed?",
     ["AnalyticsService.student_analytics"], "student"),
    ("student_lateness", "How often is Asha late?",
     ["AnalyticsService.student_analytics"], "student"),
    ("student_trend", "Is Asha improving?",
     ["AnalyticsService.student_analytics"], "student"),
    ("class_rate", "What is the attendance rate for CSE-A?",
     ["AnalyticsService.class_analytics"], "class"),
    ("attention", "Who needs attention in CSE-A?",
     ["AnalyticsService.attention_indicators"], "attention"),
    ("session", "How was the session on 2026-03-02?",
     ["AnalyticsService.overview"], "session"),
    ("overview", "How is attendance overall?",
     ["AnalyticsService.overview"], "overview"),
]
