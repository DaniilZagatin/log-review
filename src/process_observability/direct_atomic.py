"""Direct atomic map mode, code-led: actions and transitions from code first, stages and facts derived after.

orientation_map(run, source, business_text, source_scope) -> (process_map, facts, fact_stage)   (host only)
build_atomic_map(run, process_map, facts, business_text) -> (process_map, facts, record)
skipped_record(run, status, reasons) -> record
priors_from_map(process_map, *, source) -> priors; check_priors(priors) -> priors
load_obligations(text, *, source) -> obligations; check_obligations(obligations) -> obligations
identity(review) -> business identity of the mode (bound by workflow._review_config)

No stage, stage order, fact or operation exists before code is read. The map stage only checks the technical
prerequisites and returns an empty provisional frame (no model call). Atomic actions are proposed from
host-verified command units (declaration_index.py, owned elsewhere) in bounded map-atomic pages, ordered from entry
units along their callees and technical relations, then the rest of the inventory. A page carries the units with the
units they call, a shared context table whose rows name the accepted operations that code already realized, the
technical relations around each unit, the exact emit sites, provisional working groups and the generated business
text, labelled as a hypothesis. The model accounts for every supplied unit, proposes actions only for code a unit
realizes, cites each source-shown transition (a handoff needs its contract literal on both sides) and states each
action's exit. The host owns every ID, cites only supplied ranges and commits a page all or nothing. A proposed
transition is active only when the host recomputes its structural support from that page's own source
(transition_support.py: one unit, a bounded control chain or a contract cited on both sides); every other proposal is
kept inactive with its reason and an addressed gap, never deleted and never used for order, entries or scenarios.

After the pages ONE compose request (the earlier composition slot) derives the stages from the accepted actions,
proposes facts at action boundaries, matches explicitly supplied human-approved obligations and checks business
hypotheses. Stage order is derived by the host from accepted operation transitions only. If composition cannot run
or fails, the provisional working groups (or one ungrouped stage) and host boundary facts are kept and the map stays
partial. The host also computes the frontier: entries, terminals, open continuations, unresolved handoffs, pending
local work and disconnected fragments, as addressed gaps; it never adds an edge for connectivity. Earlier coarse
event mappings (priors) are evidence-free hints on exact sites: never operations, never coverage. Optional triage
sorts units from descriptors.

Bounds are planning bounds of the local request: the full system text (prompt and guidance), the user JSON and the
reserve for the one validation repair are counted, as UTF-8 bytes (a conservative upper bound of tokens) or with
the configured local tokenizer. The planned answer size is a page-sizing estimate; it never changes the provider
output limit of the model profile. Validation establishes identity, sources and complete accounting, never
semantic truth.
"""

from collections import Counter
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from .action_catalog import _site, candidate_areas
from .action_refinement import (INITIATIONS, OPERATION_KINDS, _COMMON, _EDGE, _HALTING_LAYERS, _KEYED, _NOT_STATIC,
                                _RESERVE, _Page, _catalog_binding, _digest, _issue, _numbers)
from .code_review import numbered_text, stable_id
from .declaration_index import CONTEXT_ORIGIN, CONTEXT_POLICY, COVERAGE_SCHEMA, ORIGIN
from .contracts import (CONTINUATION_MODES, EFFECT_KINDS, EXTERNAL_DIRECTIONS, JOIN_MODES, LIFECYCLE_ROLES,
                        OUTCOME_KINDS, Applicability, ArtifactStatus, Assessment, CodeExplanation, ContractError,
                        EventMapping, ExternalInteraction, FindingState, OperationDetails, OperationTransition,
                        Opportunities, ProcessMap, ProcessOperation, ProcessScenario, RequiredFact, Stage, Transition,
                        strict_json_loads, validate_evidence_links)
from .source_tools import MAX_CITATION_LINES, blocked_path, covering_ranges
from . import atomic_sources
from .atomic_sources import AtomicSources, SourceRequestError
from .tracing import safe_data
from . import transition_support
from .transition_support import (CONTROL_KINDS, MAX_LINKS, OPEN_KINDS, POLICY as TRANSITION_POLICY, classify,
                                 gap_id, still_open, unsupported_pairs)
from .workflow import (_FEEDBACK_CHARS, _PARTIAL, _PROCESS, _PROPOSED, _REF, _REPAIR_SYSTEM, _REASONS, _ROLES, _ROOT, _RULES,
                       _UNOBSERVED, ATOMIC_INPUT_CEILING, MAX_ATOMIC_PAGE_UNITS, OptionalObject, ProposalIssue,
                       _PhaseError, _build, _choice, _conform, _failure, _join, _json_type, _logging_summary, _render,
                       _unique, _unique_ids, save_json)

SCHEMA = "atomic-map/2"  # /2: code-led record (code_led replaces the skeleton); /1 records stay exportable
PRIORS_SCHEMA = "atomic-priors/1"
OBLIGATIONS_SCHEMA = "approved-obligations/1"
CHECKPOINT_SCHEMA = "atomic-page-checkpoint/1"
# /3: optional, source-grounded operation details on the same atomic page (R08).
# /4: bounded local omission of two auxiliary details failures after the one repair (see _AUXILIARY).
# /5: host primary units of unanchored callable declarations (declaration_coverage.json) and open source scope.
# /6: the same bounded local recovery may also omit an unassigned supplied event site (see _UNASSIGNED_SITE).
# /7: callable bodies anchored only by context findings are primary work under their own command unit
#     (declaration_coverage policy context-callables/1, origin context_anchor).
# /8: wire decoding WIRE_DECODING reads an omitted inapplicable nullable accounting key as null in a copy (see
#     _wire_defaults); prompts and shapes are unchanged, so earlier exact requests stay comparable.
# /9: code-led map: no skeleton; working groups, exits and source-only transitions on pages; host-supplied operation
#     identities across pages; compose derives stages and facts; host frontier and addressed gaps.
# /11: bounded source choices/current-map feedback and source-bound withdrawals in the same atomic loop.
# /10: transition support (transition_support.POLICY): a page transition is active only with host-recomputed
#      structural support from that page's source; the others stay inactive proposals with addressed gaps. Page
#      prompts and shapes are unchanged; the compose prompt and context label unsupported proposals.
VALIDATOR_VERSION = "direct-atomic-validator/11"
# The validator identity covers this file and the support helper it applies.
VALIDATOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()
                                  + Path(transition_support.__file__).read_bytes()
                                  + Path(atomic_sources.__file__).read_bytes()).hexdigest()
SOURCE_POLICY = "atomic-source-investigation/1"
SOURCE_ACTION_LIMIT = 8  # observations before one semantic commit; all consume the SAME planned request budget
_REASONS["source_invalid_transition_review"] = "A transition withdrawal needs an exact prior ID and current source citations."
_REASONS.update({"source_" + key: value for key, value in {
    "observation_with_mapping": "A source observation cannot simultaneously claim semantic unit decisions.",
    "observation_not_deliverable": "No later atomic request remains; decide this fragment using current context.",
    "observation_limit": "Use the supplied source to map the fragment or leave an addressed question.",
    "already_supplied": "This identical observation is already supplied in this unfinished fragment.",
    "inapplicable_argument": "Only the selected source tool's arguments may be non-null.",
    "invalid_bound": "Use integer paging and line bounds within the reported limits.",
    "invalid_literal_query": "Use a nonblank literal query of at most256 characters, not a regex.",
    "outside_frozen_snapshot": "Read only a normalized relative path in the selected frozen snapshot.",
    "read_exceeds_line_limit": "Request a smaller exact source range; source is never truncated.",
    "observation_exceeds_char_limit": "Request a smaller page or exact range; no partial output was delivered.",
    "unknown_unit_id": "Use an exact source unit ID from the host's paged index or search results.",
    "snapshot_changed": "The selected frozen snapshot changed; observations cannot be transferred to it.",
    "invalid_map_paging": "Map inspection uses offset>=0 and an integer limit1..64.",
    "invalid_map_section": "Inspect operations, transitions or gaps of the current construction state.",
    "observation_exceeds_request_budget": "The complete observation does not fit the next model request; "
                                         "use a smaller range or inspect page. Nothing was published.",
}.items()})
DISPOSITIONS = ("realized", "supporting", "excluded", "outside_process", "unresolved")
EXCLUSIONS = ("read_only", "configuration", "duplicate_unit", "technical_infrastructure", "other")
TRIAGE_DECISIONS = ("atomic_review", "supporting", "outside_process", "unresolved")
DEPENDENCIES_PER_UNIT = 8   # direct dependencies supplied per primary unit; the rest are named with an address
CONTROL_CONTEXT_DEPTH = 2  # bounded navigation closure; tests may disable it to exercise omitted-source refusal
SHARED_CONTEXT_ROWS = 40    # rows of the shared context table of one page; more splits the page
DECLARATION_LINES = 120     # a declaration up to this size is supplied whole, otherwise its finding/site ranges
TRIAGE_PAGE_UNITS = 150
KNOWN_OPERATIONS = 80       # latest accepted operations shown on a page; the rest are counted as omitted
RELEVANT_OPERATIONS = 80    # plus accepted operations of the page's dependencies, callers and relation targets
RELATIONS_PER_UNIT = 8      # technical relation rows shown per page unit; the rest are counted
CALLER_SYMBOLS = 10
EXITS = ("terminal", "continues", "external_handoff", "unknown")
TRANSITION_BASES = ("source_condition", "source_call", "source_handoff")
OBLIGATION_STATUSES = ("matched", "partial", "not_found")
HYPOTHESIS_STATUSES = ("supported", "contradicted", "not_found", "unclear")
GAP_KINDS = ("open_continuation", "unresolved_handoff", "unknown_continuation", "pending_local_work",
             "unsupported_transition", "unresolved_control_link")
BUDGET_BLOCKED = "required_context_exceeds_request_budget"  # status reason: no page fits the explicit budget
UNGROUPED = "ungrouped"
_DEPENDENCY_KINDS = frozenset({"call", "dispatch", "callback", "data_handoff", "control_handoff", "static_call"})
# Planned answer size, in estimated tokens: a page-sizing estimate, not a provider output limit. Per unit it holds
# the decision, its actions and their optional details (R08 raised it from 300).
ANSWER_BASE, ANSWER_PER_UNIT, ANSWER_PER_SITE = 300, 450, 150
CHECKPOINTS = Path("business_checkpoints") / "map-atomic"
_OUTCOMES_ACCEPTED = frozenset({"accepted", "accepted_after_repair", "accepted_from_checkpoint",
                                "accepted_after_local_revalidation"})
# Local recovery after the one repair of an atomic page: only these two failures of one optional auxiliary details
# item, at exactly these paths, let the host omit that item from a COPY of the repair reply and re-run the complete
# shape and page validation. Every other failure (core fields, sources, IDs, units, stages, facts, transport) stays
# a rejected page; nothing is fuzzy-matched and no further model call is made.
_AUXILIARY = {
    "external_without_contract": (
        "external_interactions", re.compile(r"operations\[(\d+)\]\.details\.external_interactions\[(\d+)\]")),
    "explanation_symbol_not_in_source": (
        "code_explanations", re.compile(r"operations\[(\d+)\]\.details\.code_explanations\[(\d+)\]\.symbol"))}
MAX_AUXILIARY_OMISSIONS = 12  # omitted items per page; one more qualifying failure keeps the page rejected
# One more omittable failure (/6): operation_choice at exactly event_mappings[i].new_operation, and only when that
# entry names neither a new nor a known operation, claims no fact, points at a supplied site with an event key and
# its role and refs are valid on their own (see _Atomic.unassigned_site). It shares the omission bound above.
_UNASSIGNED_SITE = re.compile(r"event_mappings\[(\d+)\]\.new_operation")
SITE_FIELD = "event_mappings"
LOCAL_REVALIDATION_SCHEMA = "atomic-local-revalidation/1"
DERIVATION = "local_auxiliary_omission"
# Composition (facts moved here from the skeleton): after the one real repair, only invalid methodology attribution
# of facts (facts[i].guidance_ids) may be omitted from a COPY of the repair reply, each with a warning; the complete
# shape and composition validation then runs again. Every other failure keeps the composition rejected.
GUIDANCE_OMISSION_SCHEMA = "compose-guidance-omission/1"
GUIDANCE_DERIVATION = "local_guidance_omission"
GUIDANCE_CODES = ("missing_guidance_ids", "null_not_allowed", "unknown_guidance_id", "wrong_type")
_GUIDANCE_PATH = re.compile(r"facts\[\d+\]\.guidance_ids(?:\[\d+\])?")
# Wire decoding (/8): in a unit_accounting item with a known disposition and a supplied unit number, an omitted
# exclusion (disposition not excluded) or question (disposition not unresolved) can only be null; a COPY of the reply
# gets that null before the shape and page validation. The reply itself, its hash and its checkpoint stay as received.
# An excluded unit without exclusion, an unresolved one without question and every other omitted key stay rejected.
WIRE_DECODING = "atomic-nullable-accounting/1"
_NULLABLE_ACCOUNTING = (("exclusion", "excluded"), ("question", "unresolved"))
_NAME_CHARS = 80
_REPAIR_BYTES = (len(("\n\n" + _REPAIR_SYSTEM).encode("utf-8")) + len(', "validation_feedback": ')
                 + 4 * _FEEDBACK_CHARS)

_EXTERNAL = {"system": "str", "purpose": "str (optional; what is requested, sent or received when the contract "
                                         "does not say it)",
             "direction": _choice(EXTERNAL_DIRECTIONS), "endpoint": "str|null", "contract": "str|null", "refs": "[REF]"}
_EXPLANATION = {"symbol": "str", "explanation": "str", "refs": "[REF]"}
_DETAILS = OptionalObject({
    "input": "str|null", "expected_result": "str|null", "precondition": "str|null", "completion_condition": "str|null",
    "effect_kind": _choice(EFFECT_KINDS), "outcomes": "[" + _choice(OUTCOME_KINDS) + "]",
    "continuation": _choice(CONTINUATION_MODES), "join": _choice(JOIN_MODES),
    "external_interactions": [_EXTERNAL], "code_explanations": [_EXPLANATION], "questions": "[str]", "refs": "[REF]"})
_OPERATION = {"name": "str", "description": "str|null", "actor": "str|null", "kind": _choice(OPERATION_KINDS),
              "group": "int|null", "initiation": _choice(INITIATIONS), "initiation_basis": "str|null",
              "exit": _choice(EXITS), "exit_question": "str|null", "refs": "[REF]", "details": _DETAILS}
_NEW_GROUP = {"name": "str", "contract": "str"}
_DETAIL_TEXTS = ("input", "expected_result", "precondition", "completion_condition")
_ACCOUNT = {"unit": "int", "disposition": _choice(DISPOSITIONS), "new_operations": "[int]", "known_operations": "[int]",
            "serves": "[int]", "exclusion": _choice(EXCLUSIONS) + "|null", "reason": "str|null", "question": "str|null"}
_MAPPING = {"site": "int", "new_operation": "int|null", "known_operation": "int|null",
            "lifecycle_role": _choice(_ROLES), "role_basis": "str|null", "occurrence_rule": "str",
            "ambiguity": "str|null", "refs": "[REF]"}
_ATOMIC_EDGE = {"from_new": "int|null", "from_known": "int|null", "to_new": "int|null", "to_known": "int|null",
                "condition": "str|null", "basis": _choice(TRANSITION_BASES), "contract": "str|null",
                "basis_note": "str", "refs": "[REF]"}
ATOMIC_SHAPE = {"operations": [_OPERATION], "new_groups": [_NEW_GROUP], "unit_accounting": [_ACCOUNT],
                "event_mappings": [_MAPPING], "operation_transitions": [_ATOMIC_EDGE], "unknowns": "[str]"}
ATOMIC_SHAPE["transition_review"] = OptionalObject({"withdrawals": [{"transition_id": "str", "reason": "str",
                                                                               "refs": "[REF]"}]})
ATOMIC_SHAPE["source_request"] = OptionalObject({
    "tool": _choice(("index", "search", "read", "read_unit", "inspect_map")), "unit_id": "str|null",
    "path": "str|null", "start_line": "int|null", "end_line": "int|null", "query": "str|null",
    "offset": "int", "limit": "int", "section": "str|null"})
_TRIAGE = {"unit": "int", "decision": _choice(TRIAGE_DECISIONS), "reason": "str|null", "question": "str|null"}
TRIAGE_SHAPE = {"units": [_TRIAGE], "unknowns": "[str]"}
# Composition (after all pages): final stages, boundary facts, obligation and hypothesis checks, display scenarios.
_STAGE = {"name": "str", "contract": "str", "operations": "[int]"}
# A fact names the roles of records that would observe it; "unknown" is no role a fact can require, so a record of
# unknown role can never confirm a composed fact.
FACT_ROLES = tuple(role for role in _ROLES if role != "unknown")
_FACT = {"stage": "int|null", "operations": "[int]", "obligation": "int|null", "meaning": "str", "rationale": "str",
         "scope": "str", "condition": "str|null", "metric_ids": "[str]", "evidence_alternatives": "[str]",
         "guidance_ids": "[loaded guidance ID]", "lifecycle_roles": "[" + _choice(FACT_ROLES) + "]"}
_OBLIGATION = {"obligation": "int", "status": _choice(OBLIGATION_STATUSES), "operations": "[int]",
               "question": "str|null"}
_HYPOTHESIS = {"statement": "str", "status": _choice(HYPOTHESIS_STATUSES), "operations": "[int]", "note": "str|null"}
_SCENARIO = {"name": "str", "description": "str|null", "operations": "[int]", "obligation": "int|null"}
_GROUP = {"name": "str", "stage": "int", "operations": "[int]"}
COMPOSE_SHAPE = {"process": _PROCESS, "stages": [_STAGE], "facts": [_FACT], "obligations": [_OBLIGATION],
                 "hypotheses": [_HYPOTHESIS], "scenarios": [_SCENARIO], "groups": [_GROUP], "unknowns": "[str]"}

# Fact rules shared by composition; stated once so the boundary distinctions stay the same wherever facts are made.
_FACT_RULES = (
    "- Each fact is one independently observable proposition at one object/system boundary. Assignment or routing "
    "to a person is not actual human work start; an automated worker starting is a third fact. Do not combine "
    "these alternatives in one meaning or let one act as evidence for another. Include these boundaries only "
    "when the accepted actions have them.",
    "- An external system's task/command/response and an internal queue item's receipt, processing or completion "
    "are distinct boundaries and identities. Separate their facts and expose any unknown join; names cannot establish "
    "a join, contract, receiver outcome or a field's business meaning. A required link between two identities may be "
    "its own identifier fact; it never merges their events.",
    "- lifecycle_roles name the lifecycle roles of the records that would directly observe the fact: result for a new "
    "completed outcome; duplicate, retry, refusal, no_op, error, request or start when the fact is about exactly that "
    "record. metric_ids name the metrics a fact would support.",
)

ATOMIC_SYSTEM = "\n".join((
    "TASK: map-atomic",
    "Propose the atomic business actions realized by the supplied host command units and the order the supplied code "
    "shows between them, and account for every supplied unit. No stage or fact exists yet: they are derived later "
    "from the accepted actions.",
    "- business_hypothesis is generated context, not authority. It never creates, orders, names into existence or "
    "confirms an action, a stage, a transition or a fact; behaviour it expects without supplied code stays out of the "
    "operations. Use it at most to choose clear names for what the code does.",
    "- working_groups are provisional groups of earlier accepted actions with a short contract. group is the number of "
    "the working group an action fits, the number of a new_groups item of this reply (numbered after the supplied "
    "groups), or null when none fits yet. Groups are regrouped later; they are not stages and give no order.",
    "- units are host-numbered command units: a code declaration verified by the host in the frozen source (status "
    "verified) or one code fragment (status tentative). findings are proposals of a separate code model; excerpts "
    "are frozen, redacted source text. role primary is a structural entry hint; supporting marks code called by "
    "another unit of this page. Both need a decision.",
    "- A unit with origin declaration_inventory is a callable declaration of the frozen source that no technical "
    "finding anchored: the host supplies its whole declaration and no findings, and owner names the enclosing "
    "declaration for navigation only. Decide it from that code like any unit: it may realize its own action, support "
    "another unit, or be excluded, outside_process or unresolved with a concrete reason or question. Being supplied "
    "is no evidence of an action. For each action it realizes, cite in refs the exact supplied lines of each separate "
    "effect; its declaration_anchor lines are only a structural navigation anchor that the host links and show no "
    "effect.",
    "- A unit with origin context_anchor is a callable declaration that technical findings marked only as context "
    "(for example an error check or an identifier), never as an action candidate: the host supplies its whole "
    "declaration with those findings. The context label decides nothing; decide the unit from its code like any unit.",
    "- An atomic action is one user interaction, one invoked command or one automatic action with its own "
    "initiation, execution boundary and distinguishable outcome. Keep distinct commands separate: adding, changing "
    "and removing items, or setting two different addresses, are different actions even when they look alike. One "
    "command whose code shows several distinct business effects may realize several actions, with the reason; "
    "alternative arguments with the same effect are one action. A condition that decides a route, a result, a wait "
    "or a retry the code shows may be its own action. Never duplicate one execution of a command. There is no target "
    "number of actions, and an action needs no logging call.",
    "- Never glue independent requests, searches, state changes or decisions that the supplied code shows separately "
    "into one generic handle, process or manage action. A getter, mapping, formatting or calculation detail is no "
    "action and no user click on its own: account it supporting or excluded with its reason.",
    "- Receiving from or handing work to a queue, topic or another component can be an automatic or external_message "
    "action of the process even when no customer acts; reading or listening code is not excluded for that alone.",
    "- Request, start, success, refusal, error, retry, duplicate delivery and no-op of one command are lifecycle "
    "roles of its event records, never separate actions; several log records of one command are one action.",
    "- A folder, file or symbol name alone decides neither an action nor its group.",
    "- Return exactly one unit_accounting item per supplied unit number. realized: new_operations and/or "
    "known_operations it realizes. supporting: code that only serves other units; serves lists the served unit "
    "numbers of this page ([] when it serves many) and reason says how. excluded: exclusion and reason. "
    "outside_process: it does not serve this process; give the reason. unresolved: the supplied text cannot decide "
    "it; give a concrete question. A name or path alone never makes code supporting: decide from the supplied code "
    "and use unresolved when unsure. A unit realizing several operations, sharing an operation with another unit or "
    "joining a known operation needs a reason. Every new operation needs at least one supplied unit that realizes it.",
    "- A shared context excerpt alone does not establish an operation source. Cite the relevant helper in the "
    "operation refs, or explicitly account a unit as supporting that operation; code excluded or belonging to "
    "another operation does not lend event support merely by appearing in this request.",
    "- shared_context rows are code that the units call, supplied once with findings and excerpts; they are "
    "context, not decisions, and may be cited for the units that use them. A row's accepted_operations are the "
    "known_operations numbers that code already realized on earlier pages; units' called_by_operations name the "
    "accepted operations of earlier callers. Link to them by known number, never by a similar name. context_omitted "
    "names dependencies that were not supplied, with an address; when such a dependency decides the meaning, use "
    "unresolved.",
    "- relations are technical relations around a unit: worker_proposal rows are a code model's proposals with refs, "
    "host_static rows are static call sites. Both are navigation: a relation, a call or a registration never "
    "establishes a transition by itself, and an unresolved or external target says nothing about the receiver.",
    "- known_operations are earlier proposals accepted for source, shape and accounting, not proof that another "
    "unit has the same business effect. Reuse one only when the supplied code implements or invokes the SAME "
    "action on the same business object, in the same source/destination system and at the same execution/lifecycle "
    "boundary; explain the concrete link in reason. The same verb, stage or generic queue method is not enough. "
    "Fetching work from an external inbox is not fetching an item from an internal queue; deferring one does not "
    "defer the other. A wrapper can share an action when the supplied code shows it invokes that same effect. "
    "If equivalence is not shown, keep distinct actions or an explicit unresolved question instead of merging.",
    "- initiation user_interaction only when a supplied excerpt actually handles the interaction: cite it in refs and "
    "explain it in initiation_basis. Never derive buttons, screens, clicks or their order from API, function or file "
    "names. Use api_call for an invoked backend command without supplied interaction source, automatic for scheduled "
    "or triggered system work, external_message for an incoming callback or message, else unknown.",
    "- operations are numbered by position, the first is 1; new_operations, new_operation, from_new and to_new use "
    "these positions, the known_ fields use known_operations numbers.",
    "- exit says what the supplied code shows after the action: terminal (the process ends here), continues (a next "
    "step exists in the analysed code), external_handoff (work goes to a receiver whose code is not supplied; "
    "exit_question names it and what stays unknown) or unknown (the supplied code cannot tell; exit_question asks). "
    "Never guess a receiver's outcome; a local callee that is not supplied yet is pending, not external.",
    "- event_mappings select supplied sites by number; only sites with a literal or resolved event key can be mapped. "
    "Take the lifecycle role from the code condition around the site: refusal (a business rule declined), error "
    "(execution failed), retry, duplicate (repeated delivery or already processed), no_op (nothing to do), request, "
    "start or result. An early return before any new effect is duplicate or no_op, never result. Every role other "
    "than unknown needs role_basis and refs to that condition; write remaining doubt in ambiguity. A duplicate, no-op, "
    "refusal or error record never shows a new result.",
    "- sites[*].earlier_proposals are labels of an earlier coarse map without evidence: hints where to look, never "
    "authority. Decide role and operation from the supplied code.",
    "- operation_transitions only where supplied code shows the order, always with refs: source_condition (a "
    "condition or sequence in the supplied code), source_call (one action's code calls, returns into or calls back the "
    "other's), source_handoff (one side sends to a queue, topic, registry key or callback name that the other side "
    "receives: contract is that literal name and refs cite it on both sides). Business text is never a basis. "
    "Independently invoked commands get no transition and stay unordered; a precondition check that an object exists "
    "is not a route. Never add a transition to connect fragments; leave the exit open instead.",
    "- details is optional for each new action: omit it or use null when the supplied code shows nothing more. When "
    "given, every field stays null, \"unknown\" or [] unless excerpts of this action's own refs, its units or the "
    "supporting units serving them show it; details.refs must cite the lines that show each stated input, "
    "expected_result, precondition, completion_condition, effect_kind other than unknown, outcome, continuation or join. "
    "Never fill a field to complete the picture; write what the code cannot settle in questions.",
    "- input and expected_result describe the implementation: what the code takes and what it is written to produce, "
    "never an observed run. precondition is a condition checked before the effect. completion_condition only when the "
    "code ends the process on a stated result. outcomes lists the alternative results the code distinguishes.",
    "- effect_kind: decision (the action itself decides), recommendation (it only proposes), command (it asks another "
    "component or system to act; execution there is not shown), result (the action itself commits the effect).",
    "- continuation choice or all and join all or any only when the cited code shows that one branch is chosen, that all "
    "branches run, or how incoming branches are awaited; otherwise unknown. Several transitions alone show none of these.",
    "- external_interactions only for an explicit call, message or callback in the cited code: system, direction "
    "request, send or receive, an endpoint or contract, optional purpose and refs. A class name alone is no call. An "
    "outgoing request or send never shows the receiver's outcome; a backend API command is no user click.",
    "- code_explanations: symbol is a function, method, class or field that occurs in its cited lines; explanation "
    "says in Russian what that code does for the action. It is an interpretation, not a verification.",
    "- Cite only supplied excerpt ranges. Prefer the smallest range that shows the effect, commit or condition; a "
    "read or lookup is not evidence of a creation or change.",
    *_COMMON,
    "Output shape:", _render(ATOMIC_SHAPE),
    f"REF: {_render(_REF)}",
    "A REF cites a supplied relative path and physical numbered lines inside excerpts of this request; touching or "
    "overlapping excerpts may be joined, never across a line no excerpt shows. Each cited range has at most "
    f"{MAX_CITATION_LINES} lines. It shows only that the text exists, not that the code runs or causes an outcome."))

TRIAGE_SYSTEM = "\n".join((
    "TASK: map-triage",
    "Sort host command units before atomic review, from their technical findings only; no source text is supplied.",
    "- Return exactly one units item per supplied unit number. atomic_review: the unit may realize or support "
    "process actions and needs a full atomic page. supporting: it only serves other code; give the reason. "
    "outside_process: it does not serve this process; give the reason. unresolved: the findings cannot decide it; "
    "give a concrete question. When unsure use atomic_review.",
    "- A unit that only reads, listens to a queue or hands work to another component may still serve the process: "
    "supporting or outside_process needs a reason from its findings, never a name alone.",
    "- business_hypothesis is generated context, never a reason to drop or keep a unit.",
    "- Do not create actions, stages, event mappings or transitions.",
    *_COMMON,
    "Output shape:", _render(TRIAGE_SHAPE)))

ATOMIC_SYSTEM += "\n" + "\n".join((
    "- Investigate a local execution path, not an inventory of integrations. Before creating isolated helper actions, "
    "find the calling entry, branch and receiver using source_tools when missing. Technical names/findings are "
    "navigation hypotheses, never the process. Trace where a call's result is consumed.",
    "- source_request is optional. To investigate first, give one tool request and leave ALL other reply arrays empty. "
    "Tools: index(query,offset,limit), search(query,offset,limit), read_unit(unit_id), read(path,start_line,end_line), "
    "inspect_map(section operations/transitions/gaps,offset,limit). Inapplicable fields are null; offset>=0, "
    "limit1..64. Index/search/map inspection are navigation only. Only a later request actually containing read "
    "text may cite it. No tool executes code, resolves names into control flow or adds actions automatically.",
    "- current_map is construction state, not observed execution. Continue existing IDs and inspect omitted registry "
    "pages. Read the missing caller/selector/receiver rather than inventing a connection or declaring local source "
    "unavailable. Real independent entries may stay separate. Once the path is understood, omit source_request, "
    "account every primary unit and map its actions AND supported continuations together. Read is not reviewed.",
    "- Each observation and repair uses the SAME existing planned request/transport limits. An unchanged observation "
    "in the same unfinished fragment is no progress; at most8 precede a semantic commit. Preserve explicit remaining "
    "work and addressed questions if bounds or source do not settle a continuation."
))


COMPOSE_SYSTEM = "\n".join((
    "TASK: map-compose",
    "Derive the stages of the process from the accepted atomic actions, propose the facts that observe their "
    "boundaries, and check the supplied obligations and hypotheses against them.",
    "- operations are accepted actions numbered by the host, each with its provisional working group, initiation, "
    "exit and the code units that realize it. transitions are orders proposed on the pages whose cited code the host "
    "found structurally connected (one unit, a short call, return or callback chain, or a contract cited on both "
    "sides); that is eligibility, not proof of the order or of its condition. unsupported_transitions were proposed "
    "but their cited code did not connect the two actions: they are no order, never a reason for a stage order or a "
    "scenario path, and stay open questions. Do not create, rename, merge or drop actions and do not cite code.",
    "- stages: every operation number appears in exactly one stage. contract says which results, decisions and "
    "outcomes of its actions belong together. Working groups are hints: keep, merge or split them where the actions "
    "show a better business boundary. Do not order stages: the host derives stage order from accepted transitions "
    "only, and an unordered map stays unordered.",
    "- facts observe accepted actions: stage is the stage number and operations the actions (all of that stage) whose "
    "boundary the fact observes; a fact without operations is only allowed for an approved obligation that no action "
    "realizes (operations [], stage null, obligation its number).",
    *_FACT_RULES,
    "- approved_obligations are human-approved requirements, separate from generated text. Give each exactly one "
    "obligations item: matched (the operations that realize it), partial (the operations that realize part; question "
    "names the rest) or not_found (operations [] and a question). Never treat an obligation as an action or a stage.",
    "- business_hypothesis is generated context. hypotheses lists statements from it that the accepted actions "
    "support, contradict or do not show (supported, contradicted, not_found, unclear) with the operation numbers "
    "concerned. A hypothesis never creates an action, stage, fact or order.",
    "- scenarios only when scenarios_requested is true, else []: a named path or alternative through accepted actions "
    "that transitions (never unsupported_transitions) support; obligation is the number of the approved obligation "
    "it realizes, or null.",
    "- groups are optional navigation groups of actions of one stage; every action of a group must belong to that "
    "stage.",
    "- process: purpose, boundaries and case_definition as the accepted actions show them; null where they do not.",
    *_COMMON,
    "Output shape:", _render(COMPOSE_SHAPE)))

_BASIS = ("Host accounting of verified command units. Decisions are model proposals bound to host-numbered units "
          "that a request actually supplied; complete accounting shows that each unit received an explicit decision, "
          "not that the decision is semantically correct. Deferred and failed units were not decided; limits of the "
          "technical review behind the catalog remain. Stages and facts are derived from accepted operations; stage "
          "order only from accepted operation transitions. Shape, source, identity and accounting checks are not "
          "semantic verification.")
_CODE_LED_BASIS = ("Code-led map: no stage, order or fact existed before code was read. Working groups are provisional; "
                   "final stages come from the composition of accepted operations, or the provisional groups when it "
                   "did not complete. Stage transitions are derived only from accepted operation transitions. A page "
                   "transition is accepted (active) only with structural support that the host recomputes from that "
                   "page's source: one unit, a control chain of at most two links between supplied units with the "
                   "call cited, or a contract cited on both sides; that is eligibility, not proof of order or "
                   "condition. Other proposals stay in transition_support as inactive, with an addressed gap. Gaps are "
                   "host-computed from exits, transitions, open control links and undecided callees; no edge is added "
                   "for connectivity. "
                   "Obligations are only the explicitly supplied human-approved items; hypotheses come from generated "
                   "text and never create structure.")
_REASONS_RU = {
    "request_limit_reached": "достигнут лимит запросов атомарных страниц",
    "unit_exceeds_request_budget": "единица не помещается в границу одного запроса даже без зависимостей",
    "answer_exceeds_budget": "планируемый ответ по единице превышает границу ответа",
    "required_context_exceeds_request_budget": "обязательный контекст страницы превышает границу запроса",
    "halted_after_transport_failure": "запросы остановлены после ошибки соединения или ограничения сервиса",
    "halted_after_config_failure": "запросы остановлены после ошибки настройки модели",
    "halted_after_resource_failure": "запросы остановлены после превышения бюджета запроса",
    "map_stage_incomplete": "технические предпосылки карты не выполнены",
    "declaration_units_unavailable": "единицы команд не построены",
    "atomic_map_failed": "внутренняя ошибка сборки атомарной карты",
    "not_planned": "единица не была запланирована",
}
# Generic reasons of an inactive transition proposal (transition_support.REASONS), as addressed questions say them.
_SUPPORT_REASONS_RU = {"source_review_withdrawal": "переход отозван при последующей проверке по исходникам",
    "endpoint_source_not_supplied": "код одного из действий не был передан на странице, где предложен переход",
    "no_source_chain": "в переданном коде нет цепочки вызова, возврата или обратного вызова не длиннее двух связей "
                       "между кодом этих действий",
    "chain_without_cited_call": "цепочка вызовов в переданном коде есть, но предложение не цитирует место вызова в "
                                "вызывающем коде",
    "handoff_one_side": "литерал контракта процитирован не в коде отправителя и получателя: нужно по месту на каждой "
                        "стороне",
}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _detail_evidence(details):
    """Every evidence ID of one OperationDetails, nested ones included; [] for None."""
    if details is None:
        return []
    return [*details.evidence_ids, *(ref for item in details.external_interactions for ref in item.evidence_ids),
            *(ref for item in details.code_explanations for ref in item.evidence_ids)]


def _symbol_in(symbol, text):
    """Whether the symbol's own name occurs as a whole token in the cited text.

    An owner prefix (Class.method, Class::method, Class#method) and a trailing "()" are allowed; only the last segment
    is matched. This is a text check, never a claim that the name resolves to that declaration.
    """
    name = symbol.strip()
    if name.endswith("()"):
        name = name[:-2]
    last = re.split(r"\.|::|#", name)[-1].strip()
    if not re.fullmatch(r"(?:[^\W\d]|\$)[\w$]*", last):
        return False
    return re.search(r"(?<![\w$])" + re.escape(last) + r"(?![\w$])", text) is not None


def _guidance_issue(issue):
    """True when a composition repair failed only at a fact's methodology attribution (facts[i].guidance_ids)."""
    return (isinstance(issue, ProposalIssue) and issue.code in GUIDANCE_CODES
            and _GUIDANCE_PATH.fullmatch(issue.path or "") is not None)


def _auxiliary_target(issue):
    """(operation index, details field, item index) when issue is one omittable auxiliary failure, else None."""
    if not isinstance(issue, ProposalIssue) or issue.layer != "contract" or issue.code not in _AUXILIARY:
        return None
    field, pattern = _AUXILIARY[issue.code]
    match = pattern.fullmatch(issue.path or "")
    return None if match is None else (int(match.group(1)), field, int(match.group(2)))


def _auxiliary_items(reply, target):
    """The details list of reply that holds the targeted item, or None."""
    if target is None:
        return None
    index, field, position = target
    operations = reply.get("operations")
    if type(operations) is not list or index >= len(operations) or type(operations[index]) is not dict:
        return None
    details = operations[index].get("details")
    if type(details) is not dict or type(details.get("questions")) is not list:
        return None
    items = details.get(field)
    return items if type(items) is list and position < len(items) else None


def _wire_defaults(reply, units):
    """(decoded, paths): reply with each inapplicable omitted nullable accounting key read as null, and their paths.

    Only a unit_accounting item that is an object with a known disposition and a unit number 1..units qualifies, and
    only while the reply has no more such items than supplied units; then an omitted exclusion (disposition not
    excluded) or question (disposition not unresolved) becomes null in a deep copy. Every other omission, type or value
    is left for the strict shape and page validation. reply is never changed; without a default it is returned as is.
    """
    items = reply.get("unit_accounting") if type(reply) is dict else None
    if type(items) is not list or len(items) > units:
        return reply, []
    decoded, paths = None, []
    for index, item in enumerate(items):
        if (type(item) is not dict or item.get("disposition") not in DISPOSITIONS
                or type(item.get("unit")) is not int or not 1 <= item["unit"] <= units):
            continue
        for key, applies in _NULLABLE_ACCOUNTING:
            if key not in item and item["disposition"] != applies:
                decoded = copy.deepcopy(reply) if decoded is None else decoded
                decoded["unit_accounting"][index][key] = None
                paths.append(f"unit_accounting[{index}].{key}")
    return (reply if decoded is None else decoded), paths


def _local_record(derived, source, api_hash, raw_hash):
    """The request's local revalidation record: hashes, omissions and outcome; never the reply text itself."""
    final = derived["final"] or {}
    return {"schema": LOCAL_REVALIDATION_SCHEMA, "derivation": DERIVATION, "after_attempt": 2, "source": source,
            "api_request_sha256": api_hash, "raw_response_sha256": raw_hash, "omissions": derived["omissions"],
            "omission_limit": MAX_AUXILIARY_OMISSIONS, "omission_limit_reached": derived["limit_reached"],
            "outcome": derived["outcome"],
            "sanitized_response_sha256": None if derived["response"] is None else _digest(derived["response"]),
            "final_layer": final.get("layer"), "final_code": final.get("code"), "final_path": final.get("path"),
            "model_calls": 0,
            "basis": "The host omitted only the listed auxiliary details items and event_mappings entries of supplied "
                     "sites without any operation or fact from a copy of the rejected repair reply and re-ran the "
                     "complete validation; the reply as received stays rejected and an omitted site stays unmapped. "
                     "Validation establishes sources and accounting, never semantic truth."}


def _derived_checkpoint(data):
    """Structure and hash bindings of a checkpoint holding a locally derived reply (the replay comes later)."""
    local, accepted = data["local_revalidation"], data["accepted"]
    return (type(local) is dict and set(local) == {"schema", "derivation", "raw_response", "raw_response_sha256",
                                                   "api_request_sha256", "omissions", "sanitized_response_sha256"}
            and local["schema"] == LOCAL_REVALIDATION_SCHEMA and local["derivation"] == DERIVATION
            and type(local["raw_response"]) is dict and local["raw_response_sha256"] == _digest(local["raw_response"])
            and type(local["api_request_sha256"]) is str
            and type(local["omissions"]) is list and 1 <= len(local["omissions"]) <= MAX_AUXILIARY_OMISSIONS
            and local["sanitized_response_sha256"] == data["response_sha256"] != local["raw_response_sha256"]
            and accepted == {"attempt": 2, "api_request_sha256": local["api_request_sha256"],
                             "response_sha256": data["response_sha256"], "derivation": DERIVATION,
                             "raw_response_sha256": local["raw_response_sha256"]})


def _omission_question(code, item):
    """The addressed host question that replaces one omitted item; the model's name is quoted, never trusted."""
    key = "system" if code == "external_without_contract" else "symbol"
    name = " ".join(str(item.get(key) if type(item) is dict else "").split())
    name = name if len(name) <= _NAME_CHARS else name[:_NAME_CHARS - 1] + "…"
    if code == "external_without_contract":
        return (f"Внешнее взаимодействие «{name}» исключено из деталей при локальной проверке: ответ не назвал ни "
                "адреса, ни контракта. Уточнить адрес или контракт по коду.")
    return (f"Пояснение к «{name}» исключено из деталей при локальной проверке: имя не найдено в процитированных "
            "строках. Уточнить, к какому коду относится пояснение.")


def _site_question(site, mapped):
    """The addressed host question for one omitted entry of an unassigned site, with the site's identity."""
    key = " ".join(str(site.get("event_key")).split())
    key = key if len(key) <= _NAME_CHARS else key[:_NAME_CHARS - 1] + "…"
    where = f"{key} ({site.get('path')}:{site.get('start_line')}, {site.get('id')})"
    if mapped:
        return (f"Место логирования {where} указано в ответе ещё раз без операции и без фактов; этот пункт исключён "
                "при локальной проверке, другие привязки этого места сохранены.")
    return (f"Место логирования {where} указано в ответе без операции и без фактов; при локальной проверке пункт "
            "исключён, запись осталась без привязки к операции. Уточнить, какой операции она принадлежит.")


def _settings(review):
    return {"mode": "atomic", "page_units": review.atomic_page_units, "max_page_units": MAX_ATOMIC_PAGE_UNITS,
            "dependencies_per_unit": DEPENDENCIES_PER_UNIT, "shared_context_rows": SHARED_CONTEXT_ROWS,
            "declaration_lines": DECLARATION_LINES, "input_tokens_target": review.atomic_input_tokens,
            "input_tokens_hard": review.atomic_input_tokens_hard, "input_tokens_ceiling": ATOMIC_INPUT_CEILING,
            "answer_tokens": review.atomic_answer_tokens,
            "answer_estimate": {"base": ANSWER_BASE, "per_unit": ANSWER_PER_UNIT, "per_site": ANSWER_PER_SITE,
                                "basis": "page-sizing estimate including optional action details; the provider "
                                         "output limit of the profile is unchanged"},
            "repairs_per_request": 1,
            "wire_decoding": {"policy": WIRE_DECODING,
                              "defaults": {"unit_accounting[i].exclusion": "null when omitted and disposition is known "
                                                                          "and not excluded",
                                           "unit_accounting[i].question": "null when omitted and disposition is known "
                                                                         "and not unresolved"},
                              "scope": "items with a supplied unit number, at most one item per supplied unit",
                              "reply": "unchanged; only a decoded copy is validated; paths are recorded",
                              "model_calls": 0},
            "auxiliary_recovery": {"codes": sorted(_AUXILIARY), "after_attempt": 2,
                                   "max_omissions": MAX_AUXILIARY_OMISSIONS, "derivation": DERIVATION,
                                   "schema": LOCAL_REVALIDATION_SCHEMA, "model_calls": 0,
                                   "unassigned_sites": {
                                       "code": "operation_choice", "path": "event_mappings[i].new_operation",
                                       "requires": "new_operation and known_operation null, a supplied site with an "
                                                   "event key, a role other than unknown only with role_basis and "
                                                   "refs, every ref inside supplied lines",
                                       "result": "the entry is omitted from a copy; the site stays in the inventory "
                                                 "without that mapping, with a question in unknowns",
                                       "max_omissions": "shared"}},
            "declaration_completion": {
                "source": "declaration_coverage.json", "schema": COVERAGE_SCHEMA, "unit_origin": ORIGIN,
                "selection": "callable declarations with a body in indexed or partial files without an exactly "
                             "bound candidate or context anchor",
                "supplied": "whole declaration text and its emit sites; no finding", "triage": "not_offered",
                "evidence": "the declaration's opening lines are a structural navigation anchor; effects need the "
                            "model's exact refs",
                "open_scope": "every source file with an unknown declaration inventory blocks complete",
                "context_units": {
                    "policy": CONTEXT_POLICY, "unit_origin": CONTEXT_ORIGIN,
                    "selection": "verified context command units whose declaration is a callable with a body in an "
                                 "indexed or partial file; the command unit ID, findings and links are kept",
                    "supplied": "whole declaration text, its findings and the emit sites inside it, or deferred as "
                                "too large",
                    "triage": "not_offered"}},
            "token_estimate": "utf8_bytes_upper_bound" if review.business_token_budget is None else "tokenizer",
            "max_requests": review.refine_max_requests, "request_chars": review.business_request_chars,
            "triage": review.triage, "triage_page_units": TRIAGE_PAGE_UNITS, "compose": review.compose,
            "known_operations": KNOWN_OPERATIONS, "relevant_operations": RELEVANT_OPERATIONS,
            "instrumentation_accounting": "instrumentation-fact-accounting/1",
            "source_investigation": {"policy": SOURCE_POLICY, "max_observations_per_fragment": SOURCE_ACTION_LIMIT,
                                     "max_lines": 120, "max_chars": 100000,
                                     "budget": "same planned request/repair/transport budget; no extra calls",
                                     "reads_are": "source observations, never semantic decisions"},
            "code_led": {
                "orientation": "host only: technical prerequisites, no model call, no stage, order or fact",
                "page_order": "entry units, then callees and technical relation targets breadth-first, then the rest "
                              "of the inventory in its order",
                "groups": "provisional working groups proposed on pages; final stages from one composition request "
                          "(scenarios only when compose is on); provisional groups and host boundary facts when it "
                          "does not complete",
                "stage_order": "derived from accepted operation transitions only",
                "transitions": {"bases": list(TRANSITION_BASES), "refs": "always", "handoff": "contract literal "
                                "in at least two distinct cited ranges",
                                "support": {
                                    "policy": TRANSITION_POLICY, "kinds": list(transition_support.SUPPORT_KINDS),
                                    "max_links": MAX_LINKS,
                                    "control_links": {"host": ["navigation", "host_static"],
                                                      "model_relation": sorted(CONTROL_KINDS)},
                                    "never_links": "data_handoff, branch, shared objects, fields or identifiers",
                                    "supplied": "every unit of a chain or contract was supplied on the proposing page",
                                    "inactive": "kept with its reason and an addressed gap; no repair, no model call, "
                                                "no order, entry, fragment, scenario or composition order",
                                    "open_links": {"unresolved_kinds": sorted(OPEN_KINDS),
                                                   "dependencies": "control-linked callees not supplied on the page"},
                                    "basis": "structural eligibility from supplied source, never semantic proof"}},
                "exits": list(EXITS), "gaps": list(GAP_KINDS),
                "identities": "shared-context, dependency, caller and relation-target units expose their accepted "
                              f"operations beyond the last {KNOWN_OPERATIONS} (at most {RELEVANT_OPERATIONS} more)",
                "composition_request": "reserved when max_requests > 1",
                "composition_guidance": {"codes": list(GUIDANCE_CODES), "after_attempt": 2, "model_calls": 0,
                                         "action": "omit_invalid_auxiliary_attribution"},
                "obligations": None if review.approved_obligations is None else {
                    "schema": OBLIGATIONS_SCHEMA, "items": len(review.approved_obligations["items"]),
                    "source_sha256": review.approved_obligations["source_sha256"]}},
            "validator": VALIDATOR_VERSION, "validator_sha256": VALIDATOR_SHA256}


def identity(review):
    """Business identity of atomic mode: prompts, shapes, validator, bounds, priors and obligations."""
    prompts = "\n".join((ATOMIC_SYSTEM, TRIAGE_SYSTEM, COMPOSE_SYSTEM))
    shapes = json.dumps([ATOMIC_SHAPE, TRIAGE_SHAPE, COMPOSE_SHAPE], sort_keys=True)
    priors, obligations = review.map_priors, review.approved_obligations
    return {"prompts_sha256": _sha(prompts), "shapes_sha256": _sha(shapes), "validator": VALIDATOR_VERSION,
            "settings": _settings(review),
            "business_model_sha256": (_sha(review.business_model_fingerprint) if review.business_model_fingerprint else None),
            "priors_sha256": None if priors is None else priors["source_sha256"],
            "obligations_sha256": None if obligations is None else obligations["obligations_sha256"]}


# Priors: earlier event -> operation mappings, evidence-free hints on exact emit sites.


def priors_from_map(process_map, *, source):
    """Normalise a saved process map (ProcessMap.to_dict) of an earlier run into retrieval priors.

    Only mappings with an established event key are kept; their labels, stage and role are copied as untrusted
    hints. Nothing here is treated as evidence or as an operation of the new map.
    """
    if type(source) is not str or not source.strip():
        raise ValueError("Priors need a nonblank source label")
    if (type(process_map) is not dict or type(process_map.get("event_mappings")) is not list
            or type(process_map.get("stages", [])) is not list or type(process_map.get("operations", [])) is not list):
        raise ValueError("Priors need a saved process map with event_mappings")
    names = {item.get("id"): item.get("name") for item in process_map.get("stages", []) if type(item) is dict}
    mappings = []
    for item in process_map["event_mappings"]:
        if type(item) is not dict:
            raise ValueError("An earlier event mapping is not an object")
        key, label, stage, role = (item.get(name) for name in ("event_key", "operation", "stage_id", "lifecycle_role"))
        operation_id = item.get("operation_id")
        if (any(type(value) is not str or not value.strip() for value in (key, label, stage))
                or role not in LIFECYCLE_ROLES or (operation_id is not None and type(operation_id) is not str)):
            raise ValueError("An earlier event mapping lacks an event key, label, stage or lifecycle role")
        stage_name = names.get(stage)
        mappings.append({"index": len(mappings) + 1, "event_key": key, "operation_label": label,
                         "operation_id": operation_id, "stage_id": stage,
                         "stage_name": stage_name if type(stage_name) is str else None, "lifecycle_role": role})
    priors = {"schema": PRIORS_SCHEMA, "source": source, "mappings": mappings,
              "basis": "Earlier coarse event mappings: retrieval hints without evidence, never operations or coverage"}
    priors["source_sha256"] = _digest({key: value for key, value in priors.items()})
    return priors


def check_priors(priors):
    if (type(priors) is not dict or set(priors) != {"schema", "source", "mappings", "basis", "source_sha256"}
            or priors["schema"] != PRIORS_SCHEMA or type(priors["mappings"]) is not list
            or priors["source_sha256"] != _digest({key: value for key, value in priors.items() if key != "source_sha256"})):
        raise ValueError("map_priors must be an unaltered atomic-priors/1 record from priors_from_map")
    return priors


# The map stage of the code-led mode: technical prerequisites only, no business frame.


def orientation_map(run, source, business_text, source_scope):
    """Host-only map stage: no model call, no stage, order or fact before any code is read.

    It checks that the technical review accepted work and returns an empty provisional map; operations,
    transitions, stages and facts are derived from accepted code operations by build_atomic_map. The generated
    business text is kept only as a labelled hypothesis for the pages and the composition. An earlier skeleton reply
    is never replayed: no skeleton request exists any more.
    """
    tasks = run.code_review["ledger"]["tasks"]
    if any(row["kind"] == "base" for row in tasks) and not any(
            row["kind"] == "base" and row["state"] == "completed" for row in tasks):
        raise _PhaseError("code review produced no accepted task; see code_review.json", "upstream",
                          "no_accepted_review_task")
    run.semantic_ranges = []  # nothing is cited at this stage
    run.atomic_skeleton = None
    process_map = _build(_ROOT, ProcessMap, **run.header, status=ArtifactStatus.DRAFT, evidence_ids=[],
                         purpose=None, boundaries=None, case_definition=None, stages=[], transitions=[],
                         event_mappings=[], unknowns=_unique([_PARTIAL] if run.partial else []))
    run.guidance_links["facts"] = {}
    return process_map, [], {}


def load_obligations(text, *, source):
    """Explicit human-approved obligations: one nonblank line each, host IDs, provenance kept; never generated."""
    if type(text) is not str or type(source) is not str or not source.strip():
        raise ValueError("Approved obligations need text and a nonblank source label")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ValueError("The approved obligations file names no obligation")
    if len(lines) > 200 or any(len(line) > 1000 for line in lines):
        raise ValueError("Approved obligations are limited to 200 lines of at most 1000 characters")
    items = [{"id": f"obl-{index:03d}", "text": line} for index, line in enumerate(lines, 1)]
    record = {"schema": OBLIGATIONS_SCHEMA, "source": source, "source_sha256": _sha(text), "items": items,
              "basis": "Supplied explicitly as human-approved; matched against accepted code operations only"}
    record["obligations_sha256"] = _digest({key: value for key, value in record.items()})
    return record


def check_obligations(obligations):
    keys = {"schema", "source", "source_sha256", "items", "basis", "obligations_sha256"}
    if (type(obligations) is not dict or set(obligations) != keys or obligations["schema"] != OBLIGATIONS_SCHEMA
            or type(obligations["items"]) is not list or not obligations["items"]
            or any(type(item) is not dict or set(item) != {"id", "text"} or type(item["text"]) is not str
                   or not item["text"].strip() for item in obligations["items"])
            or len({item["id"] for item in obligations["items"]}) != len(obligations["items"])
            or obligations["obligations_sha256"] != _digest({key: value for key, value in obligations.items()
                                                             if key != "obligations_sha256"})):
        raise ValueError("approved_obligations must be an unaltered approved-obligations/1 record from load_obligations")
    return obligations


# Records.


def _units_binding(units):
    if units is None:
        return None
    return {"file": "command_units.json", "schema": units["schema"], "units_sha256": units["units_sha256"],
            "index_sha256": units["index_sha256"], "catalog_sha256": units["catalog_sha256"]}


def _coverage_binding(coverage):
    """Identity and counts of declaration_coverage.json in the record; the document itself stays beside it."""
    if coverage is None:
        return None
    scope = coverage["open_scope"]
    binding = {"file": "declaration_coverage.json", "schema": coverage["schema"],
               "coverage_sha256": coverage["coverage_sha256"], "snapshot_id": coverage["snapshot_id"],
               "index_sha256": coverage["index_sha256"], "catalog_sha256": coverage["catalog_sha256"],
               "units_sha256": coverage["units_sha256"], "primary_units": len(coverage["primary_units"]),
               "open_scope": {"files": len(scope["files"]), "blocking": scope["blocking"]}}
    if "policy" in coverage:
        binding.update(policy=coverage["policy"], context_units=len(coverage["context_units"]))
    return binding


def _anchor_lines(first, last):
    """The opening lines of a declaration that the host links as a structural anchor (never an effect)."""
    return first, min(last, first + MAX_CITATION_LINES - 1)


def _symbol(unit):
    if unit.get("declaration") is not None:
        return unit["declaration"]["qualname"]
    return unit["members"][0].get("symbol") or "<module>"


def _head(unit):
    return {"symbol": _symbol(unit), "path": unit["path"], "status": unit["status"], "unit_reason": unit["reason"],
            "candidate_ids": list(unit["candidate_ids"]), "finding_ids": list(unit["finding_ids"])}


def _state(state, reason=None, request_id=None):
    return {"state": state, "disposition": None, "operation_ids": [], "stage_ids": [], "serves_unit_ids": [],
            "exclusion": None, "reason": reason, "question": None, "request_id": request_id}


def _empty_record(run, status, reasons):
    units = run.command_units
    coverage = getattr(run, "declaration_coverage", None) if units is not None else None
    heads, candidates = {}, {}
    if units is not None:
        for unit in units["units"]:
            if unit["role"] == "candidate":
                heads[unit["id"]] = {**_head(unit), **_state(None)}
                for candidate in unit["candidate_ids"]:
                    candidates[candidate] = {"unit_id": unit["id"]}
    # Host primary units of unanchored declarations need a decision too; they own no catalog candidate.
    for unit in (coverage or {}).get("primary_units", []):
        heads[unit["id"]] = {**_head(unit), **_state(None), "origin": ORIGIN, "declaration_id": unit["declaration_id"]}
    # So do callable bodies anchored only as context: one state under the existing command unit, its findings kept.
    if coverage is not None and coverage.get("context_units"):
        command = {unit["id"]: unit for unit in units["units"]}
        for view in coverage["context_units"]:
            heads[view["id"]] = {**_head(command[view["id"]]), **_state(None), "origin": CONTEXT_ORIGIN,
                                 "declaration_id": view["declaration_id"]}
    from .technical_relations import binding as relations_binding
    obligations = run.review.approved_obligations
    record = {"schema": SCHEMA, "status": status, "status_reasons": list(reasons),
              "catalog": _catalog_binding(run.action_catalog), "units": _units_binding(units),
              "settings": _settings(run.review),
              "code_led": {"working_groups": [], "stages": {"source": "none", "items": [], "stage_transitions": []},
                           "facts": [],
                           "obligations": None if obligations is None else {
                               **{key: obligations[key] for key in ("schema", "source", "source_sha256",
                                                                    "obligations_sha256")},
                               "items": [{**item, "status": "not_checked", "operation_ids": [], "question": None,
                                          "fact_ids": []} for item in obligations["items"]]},
                           "hypotheses": [], "gaps": [],
                           "frontier": {"entries": [], "terminals": [], "fragments": []},
                           "technical_relations": relations_binding(getattr(run, "technical_relations", None)),
                           "transition_support": _support_record([], []),
                           "basis": _CODE_LED_BASIS},
              "priors": None, "triage": {"mode": run.review.triage, "status": "not_run", "request_ids": []},
              "compose": {"enabled": run.review.compose, "status": "not_run", "request_id": None},
              "requests": [], "unit_states": heads, "candidates": candidates, "operations": [], "event_mappings": [],
              "transitions": [], "prior_diagnostics": [], "required_scenarios": [],
              "groups": [], "summary": {}, "basis": _BASIS}
    if coverage is not None:
        record["declaration_coverage"] = _coverage_binding(coverage)
    return record


_SUPPORT_BASIS = ("Every transition a page proposed, with its request, raw basis, condition, citations and the host's "
                  "structural state. active: one unit, a control chain of at most two links between units supplied on "
                  "that page with the call cited, or a contract cited on both sides; inactive: kept with a generic "
                  "reason and an addressed gap, never used for order, entries, fragments, scenarios or the composition "
                  "order. open_links: unresolved dispatch, callback or control relations and control-linked callees "
                  "not supplied on the realizing action's page. Structural eligibility only: conditions, branch versus "
                  "sequence, registry mappings and outcome meaning stay model text; model relations stay proposals.")


def _support_record(proposals, open_links, reviews=()):
    """The transition-support part of a code-led record: every proposal and open control link, in commit order."""
    return {"policy": TRANSITION_POLICY, "proposals": copy.deepcopy(list(proposals)),
            "open_links": copy.deepcopy(list(open_links)), "basis": _SUPPORT_BASIS,
            **({"reviews": copy.deepcopy(list(reviews))} if reviews else {})}


def skipped_record(run, status, reasons):
    """A record without accepted pages: map stage incomplete, units unavailable or an internal failure."""
    record = _empty_record(run, status, reasons)
    reason = {"not_run": "map_stage_incomplete"}.get(status, reasons[0].split(":", 1)[0] if reasons else status)
    for head in record["unit_states"].values():
        head.update(_state("deferred", reason))
    _summarise(record, catalog=run.action_catalog)
    return record


def _percentiles(values):
    if not values:
        return None
    ordered = sorted(values)
    pick = lambda share: ordered[min(len(ordered) - 1, int(share * (len(ordered) - 1) + 0.5))]
    return {"p50": pick(0.5), "p90": pick(0.9), "max": ordered[-1], "count": len(ordered)}


def _omitted(requests):
    """Auxiliary details items omitted from locally revalidated (accepted) atomic replies."""
    return sum(len(item["local_revalidation"]["omissions"]) for item in requests
               if (item.get("local_revalidation") or {}).get("outcome") == "accepted")


def _summarise(record, *, catalog=None, appearances=None, units_without_logs=None):
    states = record["unit_states"].values()
    atomic = [item for item in record["requests"] if item["kind"] == "atomic"]
    hosts = [state for state in states if state.get("origin") == ORIGIN]
    deferred = Counter(state["reason"] for state in states if state["state"] == "deferred")
    failed = Counter(state["reason"] for state in states if state["state"] == "request_failed")
    decisions = sum(state["state"] == "accounted" for state in states)
    appearances = appearances or Counter()
    record["summary"] = {
        "candidates": len(record["candidates"]), "units": len(record["unit_states"]), "decisions": decisions,
        "triaged": sum(state["state"] == "triaged" for state in states),
        "context_appearances": sum(appearances.values()), "context_units": len(appearances),
        "duplication_ratio": round(sum(appearances.values()) / decisions, 3) if decisions else None,
        "units_without_logs": units_without_logs or 0,
        "requests": len(record["requests"]),
        "requests_by_kind": dict(sorted(Counter(item["kind"] for item in record["requests"]).items())),
        "requests_by_outcome": dict(sorted(Counter(str(item["outcome"]) for item in record["requests"]).items())),
        "dispositions": dict(sorted(Counter(state["disposition"] for state in states
                                            if state["state"] in ("accounted", "triaged")).items())),
        "deferred_by_reason": dict(sorted(deferred.items())), "failed_by_reason": dict(sorted(failed.items())),
        "operations_added": len(record["operations"]), "mappings_added": len(record["event_mappings"]),
        "mappings_by_role": dict(sorted(Counter(item["lifecycle_role"] for item in record["event_mappings"]).items())),
        "mappings_by_support": dict(sorted(Counter(item["role_support"] for item in record["event_mappings"]).items())),
        "transitions_added": len(record["transitions"]),
        "required_scenarios_unresolved": sum(item["status"] == "unresolved" for item in record["required_scenarios"]),
        "auxiliary_omissions": _omitted(record["requests"]),
        "declaration_units": len(hosts),
        "declaration_units_by_state": dict(sorted(Counter(str(state["state"]) for state in hosts).items())),
        "input_tokens": _percentiles([item["input_tokens"] for item in atomic]),
        "planned_answer_tokens": _percentiles([item["planned_answer_tokens"] for item in atomic]),
        "technical_review_complete": None if catalog is None else catalog["review"]["complete"],
        "basis": "Counts show host accounting and planned sizes, not semantic correctness or complete review."}
    if "context_units" in (record.get("declaration_coverage") or {}):  # counted apart from declaration_units
        contexts = [state for state in states if state.get("origin") == CONTEXT_ORIGIN]
        record["summary"].update(context_primary_units=len(contexts), context_primary_units_by_state=dict(sorted(
            Counter(str(state["state"]) for state in contexts).items())))
    led = record["code_led"]
    record["summary"].update(
        stages_source=led["stages"]["source"], stages=len(led["stages"]["items"]),
        stage_transitions=len(led["stages"]["stage_transitions"]), working_groups=len(led["working_groups"]),
        facts=len(led["facts"]), gaps=len(led["gaps"]),
        gaps_by_kind=dict(sorted(Counter(item["kind"] for item in led["gaps"]).items())),
        entries=len(led["frontier"]["entries"]), fragments=len(led["frontier"]["fragments"]),
        obligations_by_status=dict(sorted(Counter(item["status"] for item in (led["obligations"] or {}).get(
            "items", [])).items())))
    support = led.get("transition_support")
    if support is not None:  # transitions_added counts the active ones; every proposal is counted here
        inactive = [item for item in support["proposals"] if item["state"] == "inactive"]
        record["summary"].update(
            transition_proposals=len(support["proposals"]), transitions_inactive=len(inactive),
            transitions_inactive_by_reason=dict(sorted(Counter(item["reason"] for item in inactive).items())),
            open_control_links=len(support["open_links"]))


# The atomic pages.


def build_atomic_map(run, process_map, facts, business_text):
    """Accepted atomic operations, transitions and mappings; then derived stages, facts, gaps (code-led)."""
    if run.command_units is None:
        reason = f"declaration_units_unavailable:{run.units_error or 'not_built'}"
        record = skipped_record(run, "failed", [reason])
        lines = ["Атомарные действия не построены: единицы команд недоступны (" + (run.units_error or "нет данных")
                 + "); каталог кандидатов сохранён в action_catalog.json."]
        return replace(process_map, unknowns=_unique([*process_map.unknowns, *lines])), facts, record
    return _Atomic(run, process_map, facts, business_text).build()


ATOMIC_SYSTEM += (
    "\nOptional transition_review.withdrawals corrects an earlier transition when newly supplied code disproves its "
    "order or condition. Use the exact ID from current_map.transitions or inspect_map, a precise reason and current "
    "source refs. The original stays in history; withdrawn edges never order downstream actions. Put a corrected "
    "source-backed edge in operation_transitions. Never withdraw merely to reduce disconnected nodes. A real call "
    "chain alone does not prove branch predicate, sequence of alternatives or committed business outcome.")

class _Atomic:
    def __init__(self, run, process_map, facts, business_text):
        self.run, self.map, self.facts, self.business = run, process_map, list(facts), business_text or ""
        self.snapshot, self.settings = run.snapshot, run.review
        self.catalog, self.units_doc = run.action_catalog, run.command_units
        self.units = {unit["id"]: unit for unit in self.units_doc["units"]}
        self.candidate_order = [unit["id"] for unit in self.units_doc["units"] if unit["role"] == "candidate"]
        # Host primary units: unanchored callable declarations of declaration_coverage.json, bound to these units.
        self.coverage = getattr(run, "declaration_coverage", None)
        if self.coverage is not None and (self.coverage["units_sha256"] != self.units_doc["units_sha256"]
                                          or self.coverage["catalog_sha256"] != self.catalog["catalog_sha256"]):
            raise ValueError("declaration coverage belongs to other command units")
        hosts = (self.coverage or {}).get("primary_units", [])
        # Context units are the command units themselves (role context, findings and links unchanged); only the
        # work order and their whole supplied body tell them apart from shared context rows.
        views = (self.coverage or {}).get("context_units", [])
        if any(self.units.get(view["id"], {}).get("role") != "context" for view in views):
            raise ValueError("declaration coverage names a context unit absent from the command units")
        self.context_ids = {view["id"] for view in views}
        self.units.update({unit["id"]: unit for unit in hosts})
        # Both kinds of source work follow the candidates in declaration index order.
        position = {item["id"]: number for number, item in
                    enumerate((getattr(run, "declaration_index", None) or {}).get("declarations", []))}
        work = sorted([*hosts, *views], key=lambda unit: position.get(unit["declaration_id"], len(position)))
        self.host_order = [unit["id"] for unit in work]
        self.order = self.candidate_order + self.host_order
        # A command unit calling an unanchored declaration (resolved same-file call) gets it as a dependency.
        self.host_callees = {}
        for unit in hosts:
            for caller in unit["navigation"]["caller_unit_ids"]:
                if self.units.get(caller, {}).get("origin") != ORIGIN:
                    self.host_callees.setdefault(caller, []).append(unit["id"])
        # Emit sites of host units come from the static inventory by containment, as catalog anchors get theirs.
        self.inventory = {}
        for site in (run.candidates or []):
            if type(site) is dict:
                item = _site(site)
                if type(item["start_line"]) is int and type(item["end_line"]) is int:
                    self.inventory.setdefault(item["path"], []).append(item)
        # Provisional working groups, proposed on pages; final stages are derived after all pages (compose).
        self.working_groups = []
        self._by_path = None
        self.guidance_omitting = False  # only while a composition copy is revalidated after its one repair
        self.obligations = run.review.approved_obligations
        # Technical relations around units: worker proposals and host static calls (technical_relations.json), mapped
        # to the smallest unit whose lines contain each end. Navigation only: they select source, never an edge.
        registry = getattr(run, "technical_relations", None) or {}
        self.relations = {}   # source unit -> [(relation, target unit or None)]
        for relation in [*registry.get("worker", []), *registry.get("host_static", [])]:
            source = self.unit_at(relation["source"].get("path"), relation["source"].get("start_line"),
                                  relation["source"].get("end_line"))
            if source is None:
                continue
            target = relation["target"]
            target_unit = (self.unit_at(target.get("path"), target.get("start_line"), target.get("end_line"))
                           if relation["resolution"] == "resolved" else None)
            self.relations.setdefault(source, []).append((relation, target_unit if target_unit != source else None))
        # Call lines of resolved declaration-index calls (caller declaration, callee declaration) -> [(path, lines)]:
        # where a navigation link is written, for the citation a transition needs on that link (transition_support).
        self.call_lines = {}
        for call in (getattr(run, "declaration_index", None) or {}).get("calls", []):
            if (type(call) is dict and call.get("status") == "resolved" and type(call.get("start_line")) is int
                    and type(call.get("end_line")) is int):
                self.call_lines.setdefault((call.get("caller_id"), call.get("callee_id")), []).append(
                    (call.get("path"), call["start_line"], call["end_line"]))
        self.record = _empty_record(run, None, [])
        self.states = self.record["unit_states"]
        self.used, self.halted, self.number = 0, None, 0
        self.ops, self.mappings, self.edges, self.unknowns = [], [], [], []  # edges: active transitions only
        self.transition_proposals, self.open_links = [], []  # every proposed transition; open control links
        self.transition_reviews = []  # source-bound withdrawals; original proposals never change
        self.staged, self.op_sources, self.texts = {}, {}, {}
        self.derived = {}  # request ID -> raw repair reply of a locally revalidated page, kept for its checkpoint
        self.appearances = Counter()
        self.scenarios, self.groups = [], []
        self.composed = None  # the validated composition reply, when it completed
        self.settings_record = self.record["settings"]
        self.proposals = self.resolve_priors()
        self.source_units = [{"id": uid, "path": unit["path"], "symbol": _symbol(unit),
                              "start_line": self.lines(unit)[0], "end_line": self.lines(unit)[1]}
                             for uid, unit in self.units.items()]
        self.sources = AtomicSources(self.snapshot, self.source_units)
        self.source_buffer, self.source_seen = [], set()
        self._research_queue = []

    # Source observation inside this same bounded atomic loop; no separate model or runtime.

    def source_context(self, context, ranges):
        context["source_tools"] = {
            "policy": SOURCE_POLICY, "actions": ["index", "search", "read", "read_unit", "inspect_map"],
            "unit_index": self.sources.index(), "remaining_before_commit": SOURCE_ACTION_LIMIT - len(self.source_buffer),
            "budget": "every observation consumes one of the same max_requests; the same sole JSON repair applies"}
        gaps, _ = self.frontier()
        context["current_map"] = {
            "operations": [{"id": op["id"], "name": op["name"], "unit_ids": list(op["unit_ids"]), "exit": op["exit"]}
                           for op in self.ops[-KNOWN_OPERATIONS:]],
            "operations_total": len(self.ops), "operations_omitted": max(0, len(self.ops) - KNOWN_OPERATIONS),
            "transitions": [{key: item[key] for key in ("id", "from", "to", "condition", "basis", "state", "reason")}
                            for item in self.effective_proposals()[-KNOWN_OPERATIONS:]],
            "transitions_total": len(self.transition_proposals),
            "transitions_omitted": max(0, len(self.transition_proposals) - KNOWN_OPERATIONS),
            "transition_reviews": copy.deepcopy(self.transition_reviews),
            "active_transition_count": len(self.edges), "open_paths": gaps[:12], "open_paths_total": len(gaps),
            "basis": "construction proposals and addressed questions, not observed business execution"}
        context["source_observations"] = []
        observed = {}
        for item in self.source_buffer:
            result = copy.deepcopy(item["result"])
            if item["request"]["tool"] in ("read", "read_unit"):
                ref = {key: result[key] for key in ("path", "start_line", "end_line")}
                if covering_ranges(ranges, **{"path": ref["path"], "first": ref["start_line"],
                                             "last": ref["end_line"]}) is not None:
                    result.pop("text")  # exact text is already in ordinary excerpts, never clipped
                    result["text_location"] = "ordinary excerpts of this request contain the complete read range"
                else:
                    ranges.append(ref)
                uid = item["unit_id"]
                if uid is not None:
                    observed.setdefault(uid, []).append((ref["path"], ref["start_line"], ref["end_line"]))
            context["source_observations"].append({"request_id": item["request_id"],
                                                    "request": item["request"], "result": result})
        return observed

    def inspect_source_map(self, section, offset, limit):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64:
            raise SourceRequestError("invalid_map_paging")
        if section == "operations":
            rows = [{"id": op["id"], "name": op["name"], "description": op["description"],
                     "unit_ids": list(op["unit_ids"]), "exit": op["exit"]} for op in self.ops]
        elif section == "transitions":
            rows = copy.deepcopy(self.effective_proposals())  # includes structural refusals and later withdrawals
        elif section == "gaps":
            rows, _ = self.frontier()
        else:
            raise SourceRequestError("invalid_map_section")
        chosen = rows[offset:offset + limit]
        end = min(len(rows), offset + len(chosen))
        result = {"section": section, "rows": chosen, "offset": offset, "total": len(rows),
                  "omitted": max(0, len(rows) - end), "next_offset": end if end < len(rows) else None,
                  "use": "construction_state_only"}
        if len(json.dumps(result, ensure_ascii=False)) > self.sources.max_chars:
            raise SourceRequestError("observation_exceeds_char_limit", maximum=self.sources.max_chars)
        return result

    def source_decision(self, page, payload):
        if payload.get("transition_review") is not None or any(payload[key] for key in ("operations", "new_groups", "unit_accounting", "event_mappings",
                                       "operation_transitions", "unknowns")):
            raise _issue("contract", "source_observation_with_mapping", "source_request",
                         "one source request and empty map arrays; source observation is not a semantic decision")
        command = payload["source_request"]
        if (type(command["offset"]) is not int or command["offset"] < 0 or type(command["limit"]) is not int
                or not 1 <= command["limit"] <= 64):
            raise _issue("reference", "source_invalid_bound", "source_request", "offset>=0 and integer limit1..64")
        if len(self.source_buffer) >= SOURCE_ACTION_LIMIT:
            raise _issue("contract", "source_observation_limit", "source_request",
                         "a semantic decision or addressed unresolved question after at most8 observations")
        allowed = {"index": {"query"}, "search": {"query"}, "read": {"path", "start_line", "end_line"},
                   "read_unit": {"unit_id"}, "inspect_map": {"section"}}
        tool = command["tool"]
        fields = {"unit_id", "path", "start_line", "end_line", "query", "section"}
        if any(command[key] is not None for key in fields - allowed[tool]):
            raise _issue("contract", "source_inapplicable_argument", "source_request",
                         "null in every argument that the selected tool does not use")
        preview = self.sources.fork()
        try:
            if tool == "index":
                result = preview.index(query=command["query"], offset=command["offset"], limit=command["limit"])
            elif tool == "search":
                result = preview.search(command["query"], offset=command["offset"], limit=command["limit"])
            elif tool == "read":
                result = preview.read(command["path"], command["start_line"], command["end_line"])
            elif tool == "read_unit":
                result = preview.read_unit(command["unit_id"])
            else:
                result = self.inspect_source_map(command["section"], command["offset"], command["limit"])
        except SourceRequestError as exc:
            raise _issue("reference", "source_" + exc.code, "source_request",
                         "a bounded tool request against the selected frozen snapshot: "
                         + json.dumps(exc.details, ensure_ascii=False)) from exc
        signature = _digest({"tool": "read", **{key: result[key] for key in
                             ("path", "start_line", "end_line")}} if tool in ("read", "read_unit") else command)
        if signature in self.source_seen:
            raise _issue("contract", "source_already_supplied", "source_request",
                         "use the existing source_observations; this range or navigation command is already supplied")
        reserve = 1 if self.settings.refine_max_requests > 1 else 0
        if self.budget_left() <= reserve:
            raise _issue("contract", "source_observation_not_deliverable", "source_request",
                         "a semantic decision now; no later atomic request remains to deliver an observation")
        uid = None
        if tool == "read_unit":
            uid = command["unit_id"]
        elif tool == "read":
            uid = self.unit_at(result["path"], result["start_line"], result["end_line"])
        if tool in ("read", "read_unit"):
            # Count novelty against model source already delivered, not just previous tool reads.
            prior = [span for row in self.record["requests"]
                     if row is self.record["requests"][-1] or row.get("outcome") in _OUTCOMES_ACCEPTED
                     for span in row.get("excerpts", [])]
            shown = {n for span in prior if span["path"] == result["path"]
                     for n in range(span["start_line"], span["end_line"] + 1)}
            wanted = set(range(result["start_line"], result["end_line"] + 1))
            result["new_lines"] = len(wanted - shown)
            result["seen_before"] = wanted <= shown
            result["novelty_basis"] = "accepted or current model request excerpts; not semantic review"
        if len(json.dumps(result, ensure_ascii=False, sort_keys=True)) > self.sources.max_chars:
            raise _issue("reference", "source_observation_exceeds_char_limit", "source_request",
                         "a complete tool result including controller metadata within the tool response bound")
        observation = {"policy": SOURCE_POLICY, "request": copy.deepcopy(command),
                       "result": result, "result_sha256": _digest(result), "unit_id": uid}
        # A valid local tool output must also fit the NEXT whole model request including its repair reserve.
        # Preview only: an oversized observation never publishes source ranges or consumes a pending unit.
        original = self.source_buffer
        prospective = list(self._research_queue)
        if uid in prospective:
            prospective.remove(uid)
            prospective.insert(0, uid)
        self.source_buffer = [*original, {"request_id": stable_id("adq", "atomic", page.key), **observation}]
        try:
            fitted = self.plan(prospective, self.number + 1)
        finally:
            self.source_buffer = original
        if type(fitted) is tuple:
            raise _issue("reference", "source_observation_exceeds_request_budget", "source_request",
                         "a tool result fitting the complete next primary/repair request without truncation")
        return {"source_observation": observation,
                "source_preview": preview, "source_signature": signature}

    def accept_source(self, entry, decided, queue):
        observation = copy.deepcopy(decided["source_observation"])
        entry["source_observation"] = observation
        self.sources.adopt(decided["source_preview"])
        self.source_seen.add(decided["source_signature"])
        self.source_buffer.append({"request_id": entry["id"], **observation})
        uid = observation["unit_id"]
        if uid in queue:  # explicit model source choice prioritises pending work, never marks it reviewed
            queue.remove(uid)
            queue.insert(0, uid)
        if self.run.trace:
            self.run.trace("direct_atomic.source_observation", {
                "id": entry["id"], "tool": observation["request"]["tool"], "unit_id": uid,
                "result_sha256": observation["result_sha256"],
                "new_lines": observation["result"].get("new_lines", 0)})

    # Unit helpers.

    def members(self, unit):
        return sorted(unit["members"], key=lambda item: (item["start_line"], item["end_line"], item["id"]))

    def lines(self, unit):
        declaration = unit.get("declaration")
        if declaration is not None:
            return declaration["start_line"], declaration["end_line"]
        members = unit["members"]
        return min(item["start_line"] for item in members), max(item["end_line"] for item in members)

    def findings(self, unit):
        found, seen = [], set()
        for member in self.members(unit):
            for finding in member.get("findings", []):
                if finding["id"] not in seen:
                    seen.add(finding["id"])
                    found.append(finding)
        return found

    def unit_sites(self, unit):
        sites = {site["id"]: site for member in unit["members"] for site in member.get("event_sites", [])}
        if self.source_work(unit):  # its whole body is supplied, so are the emit sites inside it
            first, last = self.lines(unit)
            sites.update((site["id"], site) for site in self.inventory.get(unit["path"], ())
                         if first <= site["start_line"] and site["end_line"] <= last)
        return sorted(sites.values(), key=lambda site: (site["path"], site["start_line"], site["end_line"], site["id"]))

    def signals(self, unit):
        counts = Counter()
        for member in unit["members"]:
            counts.update(member.get("signals", {}))
        return dict(sorted(counts.items()))

    def source_work(self, unit):
        """A host declaration unit or a context unit decided as primary work: supplied whole or deferred."""
        return unit.get("origin") == ORIGIN or unit["id"] in self.context_ids

    def primary(self, unit):
        callers = [item for item in unit["navigation"]["caller_unit_ids"]
                   if self.units.get(item, {}).get("role") in ("candidate", "declaration") or item in self.context_ids]
        return self.signals(unit).get("entry_point", 0) > 0 or not callers

    def dependencies(self, unit):
        """Direct dependencies in source order: (kept, omitted). Same-owner navigation never retrieves source.

        Resolved technical relations (worker proposals and host static calls) add their target units: they only select
        source for the page; no relation becomes a transition.
        """
        related = [target for relation, target in self.relations.get(unit["id"], ())
                   if target is not None and relation["kind"] in _DEPENDENCY_KINDS]
        ids = [item for item in dict.fromkeys([*unit["navigation"]["callee_unit_ids"],
                                               *self.host_callees.get(unit["id"], []), *related])
               if item in self.units and item != unit["id"]]
        control = set(self.control_dependencies(unit["id"]))
        ids.sort(key=lambda item: (item not in control, self.units[item]["path"], self.lines(self.units[item])[0], item))
        return ids[:DEPENDENCIES_PER_UNIT], ids[DEPENDENCIES_PER_UNIT:]

    def control_dependencies(self, identifier):
        """Resolved control navigation only; data links and lexical names never supply an execution relation."""
        unit = self.units[identifier]
        values = [*unit["navigation"]["callee_unit_ids"], *self.host_callees.get(identifier, [])]
        values += [target for relation, target in self.relations.get(identifier, ()) if target is not None
                   and (relation["origin"] == "host_static" or relation["kind"] in CONTROL_KINDS)]
        return sorted({value for value in values if value in self.units and value != identifier},
                      key=lambda value: (self.units[value]["path"], self.lines(self.units[value])[0], value))

    def control_callers(self, identifier):
        found = [uid for uid in self.units[identifier]["navigation"]["caller_unit_ids"] if uid in self.units]
        found += [source for source, rows in self.relations.items() if any(
            target == identifier and (relation["origin"] == "host_static" or relation["kind"] in CONTROL_KINDS)
            for relation, target in rows)]
        return list(dict.fromkeys(uid for uid in found if uid != identifier))

    def unit_at(self, path, first, last):
        """The smallest unit whose lines contain path:first-last, or None (relation ends to units)."""
        if type(path) is not str or type(first) is not int or type(last) is not int:
            return None
        if self._by_path is None:
            self._by_path = {}
            for identifier, unit in self.units.items():
                self._by_path.setdefault(unit["path"], []).append(identifier)
        best = None
        for identifier in self._by_path.get(path, ()):
            start, end = self.lines(self.units[identifier])
            if start <= first and last <= end and (best is None or end - start < best[0]):
                best = (end - start, identifier)
        return None if best is None else best[1]

    def callers(self, identifier):
        """Units that call or relate to this unit: navigation callers and relation sources, in a stable order."""
        unit = self.units[identifier]
        found = [item for item in unit["navigation"]["caller_unit_ids"] if item in self.units]
        found += [source for source, rows in self.relations.items() if any(target == identifier for _, target in rows)]
        return [item for item in dict.fromkeys(found) if item != identifier]

    def work_order(self):
        """Entry units first, each followed by what it reaches through dependencies; then the rest of the inventory.

        Entry: an entry-point signal or no caller (primary). Several independent entries are kept apart; reachability
        only orders the pages and never becomes a transition. Every unit of the inventory stays in the queue.
        """
        order = self.candidate_order + self.host_order
        work, queue, seen = set(order), [], set()
        for entry in [item for item in order if self.primary(self.units[item])]:
            frontier = [entry]
            while frontier:
                identifier = frontier.pop(0)
                if identifier in seen:
                    continue
                seen.add(identifier)
                queue.append(identifier)
                kept, omitted = self.dependencies(self.units[identifier])
                frontier.extend(item for item in kept + omitted if item in work and item not in seen)
        return queue + [item for item in order if item not in seen]

    def safe(self, path, first, last):
        source = self.snapshot.files.get(path)
        return (source is not None and not blocked_path(path) and type(first) is int and type(last) is int
                and 1 <= first <= last <= len(source.text.splitlines()))

    def spans(self, unit, whole):
        """Supplied ranges: the whole declaration when small enough, else its finding refs and emit sites.

        A host declaration unit has no finding ranges: its own text is its source, supplied whole on its page and
        as shared context when it is small enough.
        """
        first, last = self.lines(unit)
        host = unit.get("origin") == ORIGIN
        spans = [(unit["path"], first, last)] if whole or (host and last - first + 1 <= DECLARATION_LINES) else []
        spans += [(ref.get("path"), ref.get("start_line"), ref.get("end_line"))
                  for finding in self.findings(unit) for ref in finding.get("refs", []) if type(ref) is dict]
        spans += [(site["path"], site["start_line"], site["end_line"]) for site in self.unit_sites(unit)]
        return [span for span in dict.fromkeys(spans) if self.safe(*span)]

    def unit_address(self, identifier):
        unit = self.units.get(identifier)
        if unit is None:
            return identifier
        first, last = self.lines(unit)
        return f"{_symbol(unit)} ({unit['path']}:{first}-{last})"

    def cited_at(self, evidence_id):
        item = self.staged.get(evidence_id) or self.run.evidence.get(evidence_id)
        anchor = getattr(item, "source_anchor", None)
        return evidence_id if anchor is None else f"{anchor.path}:{anchor.start_line}-{anchor.end_line}"

    def address(self, identifier, reason):
        unit = self.units[identifier]
        first, last = self.lines(unit)
        return {"path": unit["path"], "symbol": _symbol(unit), "start_line": first, "end_line": last,
                "role": unit["role"], "reason": reason}

    def known_for(self, entries):
        """(known operations of a page, relevant ones omitted): the latest window plus accepted operations of the
        page's dependencies, callers and relation targets, in accepted order. Host identities only, bounded."""
        related = [item["unit_id"] for item in self.source_buffer if item["unit_id"] is not None]
        for entry in entries:
            unit = self.units[entry["uid"]]
            kept, omitted = self.dependencies(unit)
            related += [*kept, *omitted, *self.callers(entry["uid"]),
                        *(target for _, target in self.relations.get(entry["uid"], ()) if target is not None)]
        relevant = list(dict.fromkeys(key for identifier in dict.fromkeys(related)
                                      for key in self.states.get(identifier, {}).get("operation_ids", [])))
        window = {op["id"] for op in self.ops[-KNOWN_OPERATIONS:]}
        extra = [key for key in relevant if key not in window]
        chosen = window | set(extra[:RELEVANT_OPERATIONS])
        return [op for op in self.ops if op["id"] in chosen], max(0, len(extra) - RELEVANT_OPERATIONS)

    def relation_rows(self, identifier, on_page, shared_index, accepted):
        """Technical relation rows of one page unit (worker proposals first), and how many were left out."""
        found = self.relations.get(identifier, [])
        rows = []
        for relation, target in found[:RELATIONS_PER_UNIT]:
            end = relation["target"]
            rows.append({"origin": relation["origin"], "kind": relation["kind"], "resolution": relation["resolution"],
                         "source_lines": [relation["source"]["start_line"], relation["source"]["end_line"]],
                         "target": end.get("qualname") or end.get("label"), "target_path": end.get("path"),
                         "condition": relation.get("condition"), "data": relation.get("data"),
                         "target_unit": on_page.get(target),
                         "target_context": shared_index[target] + 1 if target in shared_index else None,
                         "target_operations": accepted(target) if target is not None else [],
                         "refs": [{key: ref[key] for key in ("path", "start_line", "end_line")}
                                  for ref in relation.get("refs", [])]})
        return rows, max(0, len(found) - RELATIONS_PER_UNIT)

    def group_number(self, group_id):
        for index, group in enumerate(self.working_groups, 1):
            if group["id"] == group_id:
                return index
        return None

    def text(self, path, first, last):
        key = (path, first, last)
        if key not in self.texts:
            self.texts[key] = numbered_text(self.snapshot.files[path], first, last)  # redacted frozen lines
        return self.texts[key]

    @staticmethod
    def finding_view(finding):
        return {"kind": finding["kind"], "description": finding["description"],
                "refs": [{key: ref[key] for key in ("path", "start_line", "end_line", "symbol") if key in ref}
                         for ref in finding.get("refs", []) if type(ref) is dict]}

    # Priors.

    def resolve_priors(self):
        priors = self.settings.map_priors
        if priors is None:
            return {}
        inventory = [_site(site) for site in (self.run.candidates or []) if type(site) is dict]
        by_key = {}
        for site in inventory:
            by_key.setdefault(site["event_key"], []).append(site)
        units_of_site = {}
        for unit in self.units_doc["units"]:
            for identifier in unit["event_site_ids"]:
                units_of_site.setdefault(identifier, []).append(unit["id"])
        resolved, proposals = [], {}
        for item in priors["mappings"]:
            matches = by_key.get(item["event_key"], [])
            static = [site for site in matches if site["key_status"] in _KEYED and site["kind"] not in _NOT_STATIC]
            status = ("key_not_found" if not matches else "no_static_site" if not static
                      else "single_site" if len(static) == 1 else "multiple_sites")
            site_ids = [site["id"] for site in static]
            resolved.append({**item, "status": status, "site_ids": site_ids,
                             "unit_ids": _unique(unit for site in site_ids for unit in units_of_site.get(site, []))})
            for site in static:
                proposals.setdefault(site["id"], []).append({
                    "prior_index": item["index"], "operation_label": item["operation_label"],
                    # The earlier map's stage label only: it names no stage of this map, which has none yet.
                    "stage_id": item["stage_id"], "stage_name": item["stage_name"],
                    "lifecycle_role": item["lifecycle_role"], "evidence": "none", "provenance": "earlier_map",
                    "sites_with_key": len(static)})
        self.record["priors"] = {"schema": priors["schema"], "source": priors["source"],
                                 "source_sha256": priors["source_sha256"], "mappings": resolved,
                                 "counts": dict(sorted(Counter(item["status"] for item in resolved).items())),
                                 "basis": priors["basis"]}
        return proposals

    # Request measurement and the request itself.

    def measure(self, system, context):
        """(first attempt, first attempt plus repair reserve, characters) of the complete request."""
        full = self.run.full_system(system)
        user = json.dumps(context, ensure_ascii=False, sort_keys=True)
        budget = self.settings.business_token_budget
        if budget is None:
            first = len(full.encode("utf-8")) + len(user.encode("utf-8"))
            reserve = _REPAIR_BYTES
        else:
            first = budget.count(full) + budget.count(user)
            reserve = budget.count("\n\n" + _REPAIR_SYSTEM) + 4 * _FEEDBACK_CHARS + 256
        return first, first + reserve, len(full) + len(user), full, user

    def within(self, system, context, *, single):
        first, repaired, chars, full, user = self.measure(system, context)
        return (chars + _RESERVE <= self.settings.business_request_chars and
                repaired <= self.settings.atomic_input_tokens_hard and
                (single or first <= self.settings.atomic_input_tokens) and
                self.run.token_fits(full, user, repair=True))

    def checkpoint_identity(self, kind, api_hash):
        shape = {"atomic": ATOMIC_SHAPE, "triage": TRIAGE_SHAPE, "compose": COMPOSE_SHAPE}[kind]
        fields = {"schema": CHECKPOINT_SCHEMA, "task": f"map-{kind}", "api_request_sha256": api_hash,
                  "shape_sha256": _sha(_render(shape)), "validator": VALIDATOR_VERSION, "mode": "atomic",
                  "settings_sha256": _digest(self.settings_record),
                  "business_model_sha256": (_sha(self.settings.business_model_fingerprint)
                                            if self.settings.business_model_fingerprint else None),
                  "snapshot_id": self.snapshot.snapshot_id,
                  "catalog_sha256": self.catalog["catalog_sha256"],
                  "units_sha256": self.units_doc["units_sha256"], "validator_sha256": VALIDATOR_SHA256}
        coverage = getattr(self, "coverage", None)
        if coverage is not None:  # the host declaration units supplied on pages belong to the identity
            fields["coverage_sha256"] = coverage["coverage_sha256"]
        return _digest(fields), fields

    def load_checkpoint(self, identifier, fields=None):
        # Unknown model identity cannot cross-reuse a business answer. No credential values enter this binding.
        if not self.settings.business_model_fingerprint or type(fields) is not dict:
            return None
        old = self.settings.resume_from
        if old is None:
            return None
        try:
            from .review_reduction import _unlinked
            path = _unlinked(Path(old) / CHECKPOINTS / f"{identifier}.json")
            if not path.is_file():
                return None
            data = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        keys = {"schema", "identity", "identity_fields", "accepted", "response_sha256", "response"}
        if (type(data) is not dict or set(data) not in (keys, keys | {"local_revalidation"}, keys | {"recovered"},
                                                      keys | {"local_revalidation", "recovered"})
                or ("recovered" in data and type(data["recovered"]) is not dict)
                or data["schema"] != CHECKPOINT_SCHEMA or data["identity"] != identifier
                or data["identity_fields"] != fields or _digest(fields) != identifier
                or type(data["accepted"]) is not dict or data["accepted"].get("attempt") not in (1, 2)
                or type(data["response"]) is not dict or data["response_sha256"] != _digest(data["response"])):
            return None
        if "local_revalidation" in data:  # request() still replays the derivation before any reuse
            return data if fields["task"] == "map-atomic" and _derived_checkpoint(data) else None
        # A plain checkpoint holds a reply as received: a derivation label without its local record is refused.
        if set(data["accepted"]) - {"attempt", "api_request_sha256", "response_sha256"}:
            return None
        return data

    def save_checkpoint(self, entry, payload):
        """Keep the accepted reply under the exact identity of its first request, for exact-hash reuse only.

        A locally derived reply is saved with the raw repair reply and its omissions, so that a later run can replay
        the derivation exactly; when redaction would change either reply, nothing is saved.
        """
        if not self.settings.business_model_fingerprint:
            entry["checkpoint"].update(saved=False, reason="business_model_identity_unavailable")
            return
        identifier, fields = entry["checkpoint"]["identity"], entry["checkpoint"]["fields"]
        response = safe_data(payload)
        document = {"schema": CHECKPOINT_SCHEMA, "identity": identifier, "identity_fields": fields,
                    "accepted": entry["accepted"], "response_sha256": _digest(response), "response": response}
        if "recovered" in entry:  # the raw reply of a prior run's attempt, kept with where it came from
            if document["response_sha256"] != entry["accepted"]["response_sha256"]:
                entry["checkpoint"].update(saved=False, reason="recovered_reply_not_replayable")
                return
            document["recovered"] = {key: value for key, value in entry["recovered"].items()
                                     if key not in ("revalidated_under", "wire_decoding")}
        local = entry.get("local_revalidation")
        if local is not None:
            raw = safe_data(self.derived[entry["id"]])
            if (_digest(raw) != local["raw_response_sha256"]
                    or document["response_sha256"] != local["sanitized_response_sha256"]):
                entry["checkpoint"].update(saved=False, reason="local_revalidation_not_replayable")
                return
            document["local_revalidation"] = {
                "schema": LOCAL_REVALIDATION_SCHEMA, "derivation": DERIVATION, "raw_response": raw,
                "raw_response_sha256": local["raw_response_sha256"], "api_request_sha256": local["api_request_sha256"],
                "omissions": local["omissions"], "sanitized_response_sha256": local["sanitized_response_sha256"]}
        try:
            directory = self.settings.directory / CHECKPOINTS
            directory.parent.mkdir(exist_ok=True)
            directory.mkdir(exist_ok=True)
            save_json(directory / f"{identifier}.json", document)
            entry["checkpoint"]["saved"] = True
        except (OSError, ValueError) as exc:
            entry["checkpoint"].update(saved=False, error=type(exc).__name__)

    @staticmethod
    def decode(kind, page, reply):
        """The copy that the validation reads (WIRE_DECODING) and its default paths; only atomic pages decode."""
        return _wire_defaults(reply, len(page.entries)) if kind == "atomic" else (reply, [])

    def record_defaults(self, entry, identifier, attempt, defaults):
        """Record and trace the exact paths read as null for the reply this request uses; nothing when none."""
        if not defaults:
            return
        entry["wire_decoding"] = {"policy": WIRE_DECODING, "attempt": attempt, "defaults": list(defaults),
                                  "model_calls": 0}
        if self.run.trace:
            self.run.trace("direct_atomic.wire_defaults", {"id": identifier, **entry["wire_decoding"]})

    def prior_reply(self, api_hash, fields):
        """A reply of a prior direct-atomic-validator/7 or /9 run for exactly this page request (resume_from), or
        None; it is revalidated completely under the current policy and keeps its prior validator as provenance."""
        old = self.settings.resume_from
        if old is None or not self.settings.business_model_fingerprint:
            return None
        from .business_replay import prior_atomic_reply
        found = prior_atomic_reply(Path(old), api_hash=api_hash, fields=fields)
        if found is not None and self.run.trace:
            self.run.trace("direct_atomic.prior_reply_found", {
                key: value for key, value in found["recovered"].items() if key != "basis"})
        return found

    def request(self, kind, system, page, shape, validate, extra=None):
        """Send one planned page with at most one validation repair; returns (request record, result or None)."""
        run = self.run
        self.used += 1
        first, repaired, chars, full, user = self.measure(system, page.context)
        api_hash = _sha(full + "\n" + user)
        identifier = stable_id("adq", kind, page.key)
        checkpoint, fields = self.checkpoint_identity(kind, api_hash)
        entry = {"id": identifier, "kind": kind, "page": page.number, "chars": chars,
                 "budget": self.settings.business_request_chars, "request_key": page.key,
                 "api_request_sha256": api_hash, "input_tokens": first, "input_tokens_with_repair_reserve": repaired,
                 "token_estimate": self.settings_record["token_estimate"],
                 "planned_answer_tokens": getattr(page, "answer", None),
                 "excerpts": [dict(item) for item in page.ranges],
                 "business_text_supplied": bool(page.context.get("business_hypothesis")), **(extra or {}),
                 "checkpoint": {"identity": checkpoint, "fields": fields, "reused": False},
                 "attempts": [], "outcome": None, "accepted": None}
        self.record["requests"].append(entry)
        if run.trace:
            run.trace("direct_atomic.request", {key: value for key, value in entry.items()
                                                if key not in ("attempts", "excerpts")})
        saved_attempt, saved_ranges = run.current_attempt, run.semantic_ranges
        run.semantic_ranges = [dict(item) for item in page.ranges]
        feedback, result, accepted = None, None, None
        try:
            stored = self.load_checkpoint(checkpoint, fields)
            if stored is None and kind == "atomic":
                # No checkpoint of this identity: an exactly bound reply of a prior direct-atomic-validator/7 or /9
                # run may still be revalidated under the current policy (business_replay.prior_atomic_reply).
                stored = self.prior_reply(api_hash, fields)
            if stored is not None:
                local = stored.get("local_revalidation")
                try:
                    if local is not None:
                        _, defaults = self.decode(kind, page, local["raw_response"])
                        derived = self.replay(page, stored, validate, shape)
                        result = derived["result"]
                    else:
                        decoded, defaults = self.decode(kind, page, stored["response"])
                        _conform(decoded, shape)
                        try:
                            result = validate(page, decoded)
                        except Exception as failure:
                            # A genuine prior repair already used the one model repair. Apply only the same
                            # bounded auxiliary recovery as the live attempt-2 path, with full validation.
                            if kind != "atomic" or stored.get("recovered", {}).get("original_attempt") != 2:
                                raise
                            derived = self.revalidate(page, decoded, failure, validate, shape)
                            if derived["outcome"] != "accepted":
                                raise failure
                            raw, raw_hash = stored["response"], stored["response_sha256"]
                            response = derived["response"]
                            accepted_hash = stored["accepted"]["api_request_sha256"]
                            local = {"schema": LOCAL_REVALIDATION_SCHEMA, "derivation": DERIVATION,
                                     "raw_response": raw, "raw_response_sha256": raw_hash,
                                     "api_request_sha256": accepted_hash, "omissions": derived["omissions"],
                                     "sanitized_response_sha256": _digest(response)}
                            stored = {**stored, "response": response, "response_sha256": _digest(response),
                                      "accepted": {"attempt": 2, "api_request_sha256": accepted_hash,
                                                   "response_sha256": _digest(response), "derivation": DERIVATION,
                                                   "raw_response_sha256": raw_hash}, "local_revalidation": local}
                            result = derived["result"]
                except Exception as exc:  # a stale or foreign checkpoint is ignored, never trusted
                    result = None
                    if run.trace:
                        run.trace("direct_atomic.checkpoint_rejected", {"id": identifier, "type": type(exc).__name__,
                                                                        "recovered": "recovered" in stored})
                else:
                    accepted = stored["response"]
                    entry["attempts"].append({"attempt": 0, "outcome": "accepted_from_checkpoint",
                                              "api_request_sha256": api_hash,
                                              "response_sha256": stored["response_sha256"],
                                              "original": stored.get("accepted")})
                    entry.update(outcome="accepted_from_checkpoint", accepted=stored.get("accepted"))
                    if local is not None:
                        self.derived[identifier] = local["raw_response"]
                        entry["local_revalidation"] = _local_record(derived, "checkpoint", local["api_request_sha256"],
                                                                    local["raw_response_sha256"])
                    self.record_defaults(entry, identifier, 0, defaults)
                    if "recovered" in stored:  # where the reply came from; the host acceptance is this request's
                        entry["recovered"] = {**stored["recovered"], "revalidated_under": VALIDATOR_VERSION,
                                              "wire_decoding": WIRE_DECODING}
                    entry["checkpoint"]["reused"] = True
            for attempt in (() if accepted is not None else (1, 2)):
                run.current_attempt = {"attempt": attempt, "max_attempts": 2, "feedback": feedback,
                                       "api_request_sha256": None}
                payload, response, decoded, defaults = None, None, None, []
                try:
                    payload = run.ask(system, page.context)
                    response = _digest(payload)
                    decoded, defaults = self.decode(kind, page, payload)
                    _conform(decoded, shape)
                    result = validate(page, decoded)
                except Exception as exc:  # user interruptions still propagate
                    failed = exc
                else:
                    accepted = payload
                    entry["accepted"] = {"attempt": attempt, "api_request_sha256": run.current_attempt["api_request_sha256"],
                                         "response_sha256": response}
                    entry["attempts"].append({"attempt": attempt, "outcome": "accepted",
                                              "api_request_sha256": run.current_attempt["api_request_sha256"],
                                              "response_sha256": response})
                    entry["outcome"] = "accepted" if attempt == 1 else "accepted_after_repair"
                    self.record_defaults(entry, identifier, attempt, defaults)
                    break
                layer, code, category, repairable, issue = _failure(failed)
                retry = ("scheduled" if repairable and attempt == 1 else "skipped_attempt_limit" if repairable
                         else "skipped_not_repairable")
                rejected = {"attempt": attempt, "outcome": "rejected", "layer": layer, "code": code,
                            "repairable": repairable, "retry": retry, "category": category,
                            "api_request_sha256": run.current_attempt["api_request_sha256"],
                            "response_sha256": response}
                if isinstance(failed, _PhaseError) and type(failed.status) is int:
                    rejected["status"] = failed.status
                if issue is not None:
                    rejected.update(issue.summary())
                if defaults:  # read as null before the failure above; the reply stays rejected as received
                    rejected["wire_defaults"] = list(defaults)
                entry["attempts"].append(rejected)
                if run.trace:
                    run.trace("validation.issue", {"stage": "map", "task": f"map-{kind}", "role": "business",
                                                   "request_id": identifier, "max_attempts": 2, **rejected})
                if retry != "scheduled":
                    entry["outcome"] = "rejected" if attempt == 1 else "rejected_after_repair"
                    result = None
                    if layer in _HALTING_LAYERS:
                        self.halted = f"halted_after_{layer}_failure"
                    elif kind == "atomic" and attempt == 2 and decoded is not None and (
                            _auxiliary_target(issue) or self.unassigned_site(issue, decoded, page)):
                        # Both attempts stay rejected as received; only a copy of the (decoded) repair reply is
                        # derived, and its wire defaults are not omissions: they do not use the omission bound.
                        derived = self.revalidate(page, decoded, failed, validate, shape)
                        self.record_defaults(entry, identifier, 2, defaults)
                        local = entry["local_revalidation"] = _local_record(
                            derived, "repair_response", rejected["api_request_sha256"], response)
                        if derived["outcome"] == "accepted":
                            result, accepted = derived["result"], derived["response"]
                            self.derived[identifier] = payload
                            entry["outcome"] = "accepted_after_local_revalidation"
                            entry["accepted"] = {"attempt": 2, "api_request_sha256": rejected["api_request_sha256"],
                                                 "response_sha256": local["sanitized_response_sha256"],
                                                 "derivation": DERIVATION, "raw_response_sha256": response}
                        if run.trace:
                            run.trace("direct_atomic.local_revalidation", {
                                "id": identifier, **{key: value for key, value in local.items() if key != "omissions"},
                                "omitted": [(item["code"], item["path"]) for item in local["omissions"]]})
                    elif kind == "compose" and attempt == 2 and decoded is not None and _guidance_issue(issue):
                        derived = self.revalidate_guidance(page, decoded, validate, shape)
                        if derived is not None:
                            local = entry["guidance_revalidation"] = {
                                "schema": GUIDANCE_OMISSION_SCHEMA, "after_attempt": 2, "model_calls": 0,
                                "api_request_sha256": rejected["api_request_sha256"], "raw_response_sha256": response,
                                "sanitized_response_sha256": (None if derived["response"] is None
                                                              else _digest(derived["response"])),
                                "outcome": derived["outcome"], "warnings": derived["warnings"],
                                "final": derived["final"]}
                            if derived["outcome"] == "accepted":
                                result, accepted = derived["result"], derived["response"]
                                entry["outcome"] = "accepted_after_local_revalidation"
                                entry["accepted"] = {"attempt": 2, "api_request_sha256": rejected["api_request_sha256"],
                                                     "response_sha256": local["sanitized_response_sha256"],
                                                     "derivation": GUIDANCE_DERIVATION,
                                                     "raw_response_sha256": response}
                            if run.trace:
                                run.trace("direct_atomic.guidance_omitted", {
                                    "id": identifier, **{key: value for key, value in local.items()
                                                         if key != "warnings"},
                                    "omitted": [(item["code"], item["path"]) for item in local["warnings"]]})
                    break
                feedback = issue.feedback(attempt, 2)
        finally:
            run.current_attempt, run.semantic_ranges = saved_attempt, saved_ranges
        if run.trace:
            run.trace("direct_atomic.request_finished", {"id": identifier, "outcome": entry["outcome"],
                                                         "attempts": len(entry["attempts"])})
        return entry, result, accepted

    # Bounded local recovery of auxiliary attribution of composed facts after the one repair (composition only).

    def revalidate_guidance(self, page, raw, validate, shape):
        """Omit invalid facts[i].guidance_ids values from a copy of the repair reply, then validate it completely.

        Mirrors the workflow's guidance fallback: valid IDs are kept, invalid ones are dropped with a warning each,
        and a fact left without IDs has unestablished attribution (never an invented rule). raw is never changed.
        Returns None when nothing qualifies, else {"outcome", "warnings", "response", "result", "final"}.
        """
        derived, warnings = copy.deepcopy(raw), []
        allowed = self.run.guidance.ids["map"]
        facts = derived.get("facts") if type(derived) is dict else None
        for index, item in enumerate(facts if type(facts) is list else []):
            if type(item) is not dict or "guidance_ids" not in item:
                continue
            field, value = f"facts[{index}].guidance_ids", item["guidance_ids"]
            if type(value) is not list:
                kept, issues = [], [(field, "null_not_allowed" if value is None else "wrong_type", _json_type(value))]
            else:
                kept = [entry for entry in value if type(entry) is str and entry in allowed]
                issues = [(f"{field}[{position}]", "unknown_guidance_id" if type(entry) is str else "wrong_type",
                           _json_type(entry)) for position, entry in enumerate(value)
                          if type(entry) is not str or entry not in allowed]
                if not value:
                    issues.append((field, "missing_guidance_ids", "array"))
            if issues:
                item["guidance_ids"] = kept
                warnings.extend({"path": path, "code": code, "actual_type": actual,
                                 "action": "omit_invalid_auxiliary_attribution", "allowed_values_count": len(allowed),
                                 "attribution": "partial" if kept else "not_established"}
                                for path, code, actual in issues)
        if not warnings:
            return None
        self.guidance_omitting = True
        try:
            _conform(derived, shape)
            result = validate(page, derived)
        except Exception as exc:  # user interruptions still propagate
            layer, code, _, _, issue = _failure(exc)
            return {"outcome": "rejected", "warnings": warnings, "response": None, "result": None,
                    "final": {"layer": layer, "code": code, "path": getattr(issue, "path", None)}}
        finally:
            self.guidance_omitting = False
        return {"outcome": "accepted", "warnings": warnings, "response": derived, "result": result, "final": None}

    def fact_guidance(self, item, path):
        """A composed fact's methodology attribution; [] only after a local omission (warned, never invented)."""
        if self.guidance_omitting and not item["guidance_ids"]:
            return []
        return self.run.guidance_ids(item, "map", path)

    # Bounded local recovery of auxiliary details after the one repair (atomic pages only).

    def revalidate(self, page, raw, failure, validate, shape):
        """Omit omittable auxiliary items from a copy of raw, one strict validation failure at a time.

        failure is the strict failure of raw itself. After each omission the complete shape and page validation runs
        again on the copy; the first failure of any other kind, or one qualifying failure more than
        MAX_AUXILIARY_OMISSIONS, ends it rejected. raw is never changed. Returns {"outcome", "omissions",
        "limit_reached", "response" (the derived reply or None), "result", "final" (the ending failure or None)}.
        """
        derived, omissions, left = copy.deepcopy(raw), [], {}
        while True:
            layer, code, _, _, issue = _failure(failure)
            target = _auxiliary_target(issue)
            items = _auxiliary_items(derived, target)
            orphan = None if items is not None else self.unassigned_site(issue, derived, page)
            if (items is None and orphan is None) or len(omissions) >= MAX_AUXILIARY_OMISSIONS:
                return {"outcome": "rejected", "omissions": omissions,
                        "limit_reached": items is not None or orphan is not None, "response": None, "result": None,
                        "final": {"layer": layer, "code": code, "path": getattr(issue, "path", None)}}
            if orphan is not None:
                # The site entry names no operation and no fact: drop just that entry. The site itself, every other
                # mapping of it and the logging inventory stay; the question says which of the two cases applies.
                position, item = orphan
                mappings = derived["event_mappings"]
                original = left.setdefault((None, SITE_FIELD), list(range(len(mappings))))
                mappings.pop(position)
                number = original.pop(position)
                site = page.sites[item["site"] - 1]
                mapped = any(other["site"] == item["site"] and (other["new_operation"] is not None
                                                                or other["known_operation"] is not None)
                             for other in mappings)
                question = _site_question(site, mapped)
                if question not in derived["unknowns"]:
                    derived["unknowns"].append(question)
                omissions.append({"field": SITE_FIELD, "original_index": number, "code": code,
                                  "path": f"event_mappings[{number}].new_operation", "item_sha256": _digest(item),
                                  "site_id": site["id"], "question": question})
            else:
                index, field, position = target
                original = left.setdefault((index, field), list(range(len(items))))
                item, number = items.pop(position), original.pop(position)
                question = _omission_question(code, item)
                questions = derived["operations"][index]["details"]["questions"]
                if question not in questions:
                    questions.append(question)
                omissions.append({"operation": index, "field": field, "original_index": number, "code": code,
                                  "path": f"operations[{index}].details.{field}[{number}]"
                                          + (".symbol" if field == "code_explanations" else ""),
                                  "item_sha256": _digest(item), "question": question})
            try:
                _conform(derived, shape)
                result = validate(page, derived)
            except Exception as exc:  # user interruptions still propagate
                failure = exc
            else:
                return {"outcome": "accepted", "omissions": omissions, "limit_reached": False, "response": derived,
                        "result": result, "final": None}

    def unassigned_site(self, issue, reply, page):
        """(position, entry) when issue is an event_mappings entry that is safe to omit, else None.

        Safe means: the strict failure is operation_choice at exactly event_mappings[i].new_operation, and that entry
        names neither a new nor a known operation, selects a supplied site with an event key, has role_basis and refs
        for any role other than unknown, and every ref cites supplied lines. Any other shape (both targets set, a
        transition) stays a rejected page. Code-led pages carry no facts; facts are derived after the pages.
        """
        if (not isinstance(issue, ProposalIssue) or issue.layer != "contract" or issue.code != "operation_choice"
                or type(reply) is not dict or type(reply.get("event_mappings")) is not list):
            return None
        match = _UNASSIGNED_SITE.fullmatch(issue.path or "")
        if match is None or int(match.group(1)) >= len(reply["event_mappings"]):
            return None
        position = int(match.group(1))
        item = reply["event_mappings"][position]
        if (type(item) is not dict or item.get("new_operation") is not None or item.get("known_operation") is not None
                or type(item.get("site")) is not int or not 1 <= item["site"] <= len(page.sites)):
            return None
        site = page.sites[item["site"] - 1]
        if type(site.get("event_key")) is not str or site.get("key_status") not in _KEYED:
            return None
        if item.get("lifecycle_role") != "unknown" and (item.get("role_basis") is None or not item.get("refs")):
            return None
        try:
            self.run.cite(item.get("refs"), {}, f"event_mappings[{position}].refs")  # a scratch registry only
        except Exception:  # an unsupplied, oversized or malformed citation is never omitted silently
            return None
        return position, item

    def replay(self, page, stored, validate, shape):
        """Derive a saved local revalidation again from its raw repair reply; any difference refuses the checkpoint."""
        local = stored["local_revalidation"]
        raw, _ = _wire_defaults(local["raw_response"], len(page.entries))  # the same decoding as the live path
        try:
            _conform(raw, shape)
            validate(page, raw)
        except Exception as exc:
            failure = exc
        else:
            raise ValueError("saved raw reply passes strict validation; nothing was derived from it")
        derived = self.revalidate(page, raw, failure, validate, shape)
        if (derived["outcome"] != "accepted" or derived["omissions"] != local["omissions"]
                or _digest(derived["response"]) != stored["response_sha256"]):
            raise ValueError("saved local revalidation is not reproduced")
        return derived

    # Unit states.

    def defer(self, identifiers, reason):
        for identifier in identifiers:
            self.states[identifier].update(_state("deferred", reason))
        if self.run.trace and identifiers:
            self.run.trace("direct_atomic.deferred", {"reason": reason, "unit_ids": list(identifiers)})

    def fail(self, identifiers, entry, reason):
        for identifier in identifiers:
            self.states[identifier].update(_state("request_failed", reason, entry["id"]))

    # Building the run.

    def build(self):
        run = self.run
        if run.trace:
            run.trace("direct_atomic.started", {"units": len(self.order), "settings": self.settings_record})
        # Entry units lead, each followed by what it reaches (callees, relation targets); every other unit of the
        # inventory follows in its order. A called unit joins its caller's page as a dependency, once.
        queue = self.work_order()
        if not queue:
            self.record["status_reasons"].append("catalog_has_no_candidates")
            if self.open_scope_blocking():
                self.record["status_reasons"].append("declaration_inventory_unknown")
            return self.finish(status="skipped_no_candidates")
        if not self.within(ATOMIC_SYSTEM, self.page([], 1).context, single=True):
            # Not even an empty page fits the explicit business budget: no request is sent, every unit stays
            # deferred, and the workflow reports the map stage as blocked by this budget (BUDGET_BLOCKED).
            self.record["status_reasons"].append(BUDGET_BLOCKED)
            self.defer(queue, BUDGET_BLOCKED)
            return self.finish()
        queue = self.triage(queue)
        self.pages(queue)
        self.compose()
        return self.finish()

    # Page planning.

    def unit_groups(self, queue, limit):
        """Up to limit groups in queue order: a lead unit with the pending candidate units it calls directly."""
        pending, claimed, groups = set(queue), set(), []
        for identifier in queue:
            if len(groups) == limit:
                break
            if identifier in claimed:
                continue
            group = [identifier]
            claimed.add(identifier)
            kept, _ = self.dependencies(self.units[identifier])
            for dependency in kept:
                if dependency in pending and dependency not in claimed:
                    group.append(dependency)
                    claimed.add(dependency)
            groups.append(group)
        return groups

    def page(self, groups, number, cited=False, closure=True):
        """The exact context of one atomic page, its citable ranges and the host maps behind its numbers."""
        entries = []
        for group in groups:
            for position, identifier in enumerate(group):
                unit = self.units[identifier]
                first, last = self.lines(unit)
                # Source work (a host declaration, a context unit) is always supplied whole or deferred as too large.
                whole = self.source_work(unit) or (not cited and last - first + 1 <= DECLARATION_LINES)
                entries.append({"uid": identifier, "number": len(entries) + 1, "lead": position == 0, "whole": whole,
                                "role": "primary" if self.primary(unit) else "supporting"})
        on_page = {entry["uid"]: entry["number"] for entry in entries}
        shared, shared_index = [], {}
        for entry in entries:
            unit = self.units[entry["uid"]]
            entry["spans"] = self.spans(unit, entry["whole"])
            kept, omitted = self.dependencies(unit)
            # omitted_ids: host IDs of the addressed omissions, for open control links (never part of the request).
            entry["context"], entry["called"], entry["omitted"], entry["omitted_ids"] = [], [], [], []
            if entry["lead"]:
                for dependency in kept:
                    if dependency in on_page:
                        entry["called"].append(on_page[dependency])
                        continue
                    if dependency not in shared_index:
                        shared_index[dependency] = len(shared)
                        shared.append({"uid": dependency, "used_by": []})
                    row = shared[shared_index[dependency]]
                    if entry["number"] not in row["used_by"]:
                        row["used_by"].append(entry["number"])
                    entry["context"].append(shared_index[dependency] + 1)
                entry["omitted"] = [self.address(item, "dependency_limit") for item in omitted]
                entry["omitted_ids"] = list(omitted)
            else:
                for dependency in kept + omitted:
                    if dependency in on_page:
                        entry["called"].append(on_page[dependency])
                    else:
                        entry["omitted"].append(self.address(dependency, "second_level_dependency"))
                        entry["omitted_ids"].append(dependency)
        # Bounded control closure around each primary body: selector and receiver code is useful source,
        # not another semantic decision and not a constructed business edge. The cited-only fallback keeps
        # the original small-page escape hatch; all absent source stays explicitly addressed.
        if not cited and CONTROL_CONTEXT_DEPTH and closure:
            for entry in entries:
                queue = [(entry["uid"], 0)]
                visited = set()
                while queue:
                    uid, depth = queue.pop(0)
                    if uid in visited or depth > CONTROL_CONTEXT_DEPTH:
                        continue
                    visited.add(uid)
                    if depth:
                        if uid not in on_page and uid not in shared_index:
                            if len(shared) >= SHARED_CONTEXT_ROWS:
                                if uid not in entry["omitted_ids"]:
                                    entry["omitted_ids"].append(uid)
                                    entry["omitted"].append(self.address(uid, "control_context_limit"))
                                continue
                            shared_index[uid] = len(shared)
                            shared.append({"uid": uid, "used_by": [], "control_lookup": True})
                        if uid in shared_index:
                            row = shared[shared_index[uid]]
                            row["control_lookup"] = True
                            if entry["number"] not in row["used_by"]:
                                row["used_by"].append(entry["number"])
                            if (shared_index[uid] + 1 not in entry["context"]
                                    and len(entry["context"]) + len(entry["called"]) < DEPENDENCIES_PER_UNIT):
                                entry["context"].append(shared_index[uid] + 1)
                    if depth < CONTROL_CONTEXT_DEPTH:
                        related = [*self.control_dependencies(uid),
                                   *(self.control_callers(uid) if depth == 0 and entry["lead"] else [])]
                        queue.extend((value, depth + 1) for value in related if value not in visited)
        if not cited and not closure and CONTROL_CONTEXT_DEPTH:
            for entry in entries:
                pending = [(entry["uid"], 0)]; seen = set()
                while pending and len(seen) < SHARED_CONTEXT_ROWS:
                    uid, depth = pending.pop(0)
                    if uid in seen or depth > CONTROL_CONTEXT_DEPTH:
                        continue
                    seen.add(uid)
                    if uid not in on_page and uid not in shared_index and uid not in entry["omitted_ids"]:
                        entry["omitted_ids"].append(uid)
                        entry["omitted"].append(self.address(uid, "control_context_budget"))
                    if depth < CONTROL_CONTEXT_DEPTH:
                        related = [*self.control_dependencies(uid),
                                   *(self.control_callers(uid) if depth == 0 and entry["lead"] else [])]
                        pending.extend((value, depth + 1) for value in related if value not in seen)
        supplied_ids = set(on_page) | set(shared_index)
        for entry in entries:
            removed = {uid for uid in entry["omitted_ids"] if uid in supplied_ids}
            entry["omitted_ids"] = [uid for uid in entry["omitted_ids"] if uid not in removed]
            addresses = {(self.units[uid]["path"], *self.lines(self.units[uid])) for uid in removed}
            entry["omitted"] = [item for item in entry["omitted"]
                                if (item["path"], item["start_line"], item["end_line"]) not in addresses]
        for row in shared:
            unit = self.units[row["uid"]]
            whole = bool(row.get("control_lookup")) and self.lines(unit)[1] - self.lines(unit)[0] + 1 <= DECLARATION_LINES
            row["spans"] = self.spans(unit, whole)
        callers = {}
        for entry in entries:
            for called_number in entry["called"]:
                callers.setdefault(called_number, []).append(entry["number"])
        # Emit sites of page units and shared rows, each once.
        sites, site_index = [], {}

        def add_site(site, unit_number=None, row_number=None):
            if site["id"] not in site_index:
                site_index[site["id"]] = len(sites)
                sites.append({**site, "units": [], "context": []})
            holder = sites[site_index[site["id"]]]
            if unit_number is not None and unit_number not in holder["units"]:
                holder["units"].append(unit_number)
            if row_number is not None and row_number not in holder["context"]:
                holder["context"].append(row_number)
        for entry in entries:
            for site in self.unit_sites(self.units[entry["uid"]]):
                add_site(site, unit_number=entry["number"])
        for index, row in enumerate(shared, 1):
            for site in self.unit_sites(self.units[row["uid"]]):
                add_site(site, row_number=index)
        spans = {}
        for path, first, last in [*(span for entry in entries for span in entry["spans"]),
                                  *(span for row in shared for span in row["spans"])]:
            spans.setdefault(path, set()).add((first, last))
        excerpts = []
        for path in sorted(spans):
            merged = []
            for first, last in sorted(spans[path]):
                if merged and first <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], last)
                else:
                    merged.append([first, last])
            excerpts += [(path, first, last) for first, last in merged]

        def covering(items):
            return [index for index, (path, first, last) in enumerate(excerpts, 1)
                    if any(path == other and first <= end and start <= last for other, start, end in items)]
        known, relevant_omitted = self.known_for(entries)
        number_of = {op["id"]: index for index, op in enumerate(known, 1)}

        def accepted(identifier):
            return [number_of[key] for key in self.states.get(identifier, {}).get("operation_ids", []) if key in number_of]
        relations_omitted = 0
        rows = []
        for entry in entries:
            unit = self.units[entry["uid"]]
            first, last = self.lines(unit)
            declaration = unit.get("declaration")
            callers_in = max((member.get("navigation", {}).get("callers_in_snapshot", 0) for member in unit["members"]),
                             default=0)
            symbols = sorted({name for member in unit["members"]
                              for name in member.get("navigation", {}).get("caller_symbols", [])})
            row = {
                "number": entry["number"], "symbol": _symbol(unit), "path": unit["path"], "status": unit["status"],
                "role": entry["role"], "start_line": first, "end_line": last,
                "declaration": None if declaration is None else {key: declaration[key] for key in
                                                                 ("qualname", "kind", "start_line", "end_line")},
                "declaration_supplied": "whole" if entry["whole"] else "cited_ranges_only",
                "signals": self.signals(unit), "findings": [self.finding_view(item) for item in self.findings(unit)],
                "excerpts": covering(entry["spans"]),
                "sites": [site_index[site["id"]] + 1 for site in self.unit_sites(unit)],
                "context": entry["context"], "called_units": entry["called"],
                "called_by": callers.get(entry["number"], []), "context_omitted": entry["omitted"],
                "navigation": {"callers_in_snapshot": callers_in, "caller_symbols": symbols[:CALLER_SYMBOLS],
                               "caller_symbols_omitted": max(0, len(symbols) - CALLER_SYMBOLS)},
                "review_gaps": [{"model_status": gap.get("model_status"), "description": gap.get("description")}
                                for member in unit["members"] for gap in member.get("review_gaps", [])],
                "entry": self.primary(unit),
                "called_by_operations": sorted({number for caller in self.callers(entry["uid"])
                                                for number in accepted(caller)})}
            row["relations"], omitted_here = self.relation_rows(entry["uid"], on_page, shared_index, accepted)
            relations_omitted += omitted_here
            if unit.get("origin") == ORIGIN:  # only host rows carry these keys; finding rows keep their shape
                anchor = _anchor_lines(first, last)
                row.update(origin=ORIGIN, owner=(unit.get("owner") or {}).get("qualname"),
                           declaration_anchor={"start_line": anchor[0], "end_line": anchor[1],
                                               "use": "structural_navigation_only"})
            elif entry["uid"] in self.context_ids:
                row["origin"] = CONTEXT_ORIGIN
            rows.append(row)
        context = {
            "task": "map-atomic",
            "working_groups": [{"number": index, "name": group["name"], "contract": group["contract"]}
                               for index, group in enumerate(self.working_groups, 1)],
            "units": rows,
            "shared_context": [{"number": index, "symbol": _symbol(self.units[row["uid"]]),
                                "path": self.units[row["uid"]]["path"],
                                "start_line": self.lines(self.units[row["uid"]])[0],
                                "end_line": self.lines(self.units[row["uid"]])[1],
                                "findings": [self.finding_view(item) for item in self.findings(self.units[row["uid"]])],
                                "excerpts": covering(row["spans"]),
                                "sites": [site_index[site["id"]] + 1 for site in self.unit_sites(self.units[row["uid"]])],
                                "used_by": row["used_by"],
                                "decision": self.states.get(row["uid"], {}).get("state") or "not_decided_yet",
                                "accepted_operations": accepted(row["uid"]),
                                **({"origin": ORIGIN} if self.units[row["uid"]].get("origin") == ORIGIN else {})}
                               for index, row in enumerate(shared, 1)],
            "sites": [{"number": index, **{key: site.get(key) for key in ("event_key", "key_status", "kind", "path",
                                                                           "start_line", "end_line", "fields")},
                       "units": site["units"], "context": site["context"],
                       "earlier_proposals": [{key: value for key, value in item.items() if key != "prior_index"}
                                             for item in self.proposals.get(site["id"], [])]}
                      for index, site in enumerate(sites, 1)],
            "known_operations": [{"number": index, "name": op["name"], "group": self.group_number(op["group_id"]),
                                  "initiation": op["initiation"], "exit": op["exit"], "description": op["description"]}
                                 for index, op in enumerate(known, 1)],
            "excerpts": [{"number": index, "path": path, "start_line": first, "end_line": last,
                          "text": self.text(path, first, last)} for index, (path, first, last) in enumerate(excerpts, 1)],
            "page": {"atomic_page": number, "units_total": len(self.order), "supplied_here": len(entries),
                     "decided_earlier": sum(state["state"] in ("accounted", "triaged") for state in self.states.values())},
            "business_hypothesis": self.business,
            "context_omissions": {"business_hypothesis": {"supplied": int(bool(self.business)), "omitted": 0,
                                                          "chars": len(self.business)},
                                  "dependencies_omitted": sum(len(entry["omitted"]) for entry in entries),
                                  "relations_omitted": relations_omitted,
                                  "known_operations": {"supplied": len(known), "omitted": len(self.ops) - len(known),
                                                       "relevant_omitted": relevant_omitted}}}
        ranges = [{"path": path, "start_line": first, "end_line": last} for path, first, last in excerpts]
        observed_units = self.source_context(context, ranges)
        page = _Page(context, ranges, number=number, entries=entries, shared=shared, sites=sites, known=list(known),
                     groups=[list(group) for group in groups], observation_units=observed_units)
        keyed = sum(type(site.get("event_key")) is str and site.get("key_status") in _KEYED for site in sites)
        page.answer = ANSWER_BASE + ANSWER_PER_UNIT * len(entries) + ANSWER_PER_SITE * keyed
        return page

    def fits(self, page, single):
        return (len(page.shared) <= SHARED_CONTEXT_ROWS and page.answer <= self.settings.atomic_answer_tokens
                and self.within(ATOMIC_SYSTEM, page.context, single=single))

    def plan(self, queue, number):
        """The largest fitting page of whole groups, or ("defer", unit, reason) for a lead that never fits."""
        groups = self.unit_groups(queue, self.settings.atomic_page_units)
        first = self.page(groups[:1], number)
        if not self.fits(first, True):
            if not groups:
                return ("defer", None, "unit_exceeds_request_budget")
            lead = groups[0][0]
            plain = self.page([[lead]], number, closure=False)
            if self.fits(plain, True):
                return plain
            alone = self.page([[lead]], number, cited=True)  # alone: no called units, finding/site ranges only
            if self.fits(alone, True):
                return alone
            reason = ("answer_exceeds_budget" if alone.answer > self.settings.atomic_answer_tokens
                      else "unit_exceeds_request_budget")
            return ("defer", lead, reason)
        low, high, best = 1, len(groups), first
        while low < high:
            middle = (low + high + 1) // 2
            page = self.page(groups[:middle], number)
            if self.fits(page, False):
                low, best = middle, page
            else:
                high = middle - 1
        return best

    def budget_left(self):
        return self.settings.refine_max_requests - self.used

    def pages(self, queue):
        # One request stays reserved for the composition that derives stages and facts, when more than one is allowed.
        reserve = 1 if self.settings.refine_max_requests > 1 else 0
        while queue:
            stop = self.halted or ("request_limit_reached" if self.budget_left() <= reserve else None)
            if stop:
                self.defer(queue, stop)
                return
            planned = self.plan(queue, self.number + 1)
            if type(planned) is tuple:
                _, lead, reason = planned
                self.defer([lead], reason)
                queue.remove(lead)
                continue
            self.number += 1
            page = planned
            identifiers = [entry["uid"] for entry in page.entries]
            taken = set(identifiers)
            self.appearances.update(row["uid"] for row in page.shared)
            extra = {"unit_ids": identifiers, "primary_unit_ids": [group[0] for group in page.groups],
                     "shared_context_unit_ids": list(dict.fromkeys([*[row["uid"] for row in page.shared],
                                                                  *page.observation_units])),
                     "site_ids": [site["id"] for site in page.sites],
                     "known_operation_ids": [op["id"] for op in page.known]}
            self._research_queue = queue
            entry, decided, payload = self.request("atomic", ATOMIC_SYSTEM, page, ATOMIC_SHAPE, self.validate, extra)
            if decided is not None and "source_observation" in decided:
                self.accept_source(entry, decided, queue)
                if not entry["checkpoint"]["reused"]:
                    self.save_checkpoint(entry, payload)
                continue
            queue[:] = [item for item in queue if item not in taken]
            if decided is None:
                self.fail(identifiers, entry, f"request_{entry['outcome']}")
                self.source_buffer, self.source_seen = [], set()
                continue
            try:
                self.commit(page, decided, entry["id"])
            except (ContractError, ValueError) as exc:  # host-side failure: nothing of this page is merged
                entry.update(outcome="commit_failed", commit_error=type(exc).__name__)
                self.fail(identifiers, entry, "commit_failed")
                self.source_buffer, self.source_seen = [], set()
                continue
            self.source_buffer, self.source_seen = [], set()
            if not entry["checkpoint"]["reused"] or "recovered" in entry:  # a recovered raw reply joins this run
                self.save_checkpoint(entry, payload)

    # Validation of one atomic reply: identity, partition and supplied sources; never semantic truth.

    @staticmethod
    def pick(item, new_key, known_key, path, new_count, known_count):
        new, old = item[new_key], item[known_key]
        if (new is None) == (old is None):
            raise _issue("contract", "operation_choice", _join(path, new_key),
                         f"int in exactly one of {new_key} or {known_key}; the other null",
                         actual="null" if new is None else "integer")
        if new is not None:
            if not 1 <= new <= new_count:
                raise _issue("reference", "unknown_new_operation", _join(path, new_key), "int (operations position)",
                             allowed=_numbers(range(1, new_count + 1)))
            return ("new", new)
        if not 1 <= old <= known_count:
            raise _issue("reference", "unknown_known_operation", _join(path, known_key),
                         "int (known_operations number)", allowed=_numbers(range(1, known_count + 1)))
        return ("known", old)

    def validate(self, page, payload):
        if payload.get("source_request") is not None:
            return self.source_decision(page, payload)
        run, staged = self.run, {}
        operations, known = payload["operations"], page.known
        supplied, new_count, known_count, site_count = len(page.entries), len(operations), len(known), len(page.sites)
        names = {group["name"].strip().casefold() for group in self.working_groups}
        for index, item in enumerate(payload["new_groups"]):
            name = item["name"].strip().casefold()
            if name in names:
                raise _issue("contract", "duplicate_group", f"new_groups[{index}].name",
                             "str (a group name that is neither supplied nor repeated; reuse the supplied number)",
                             actual="string")
            names.add(name)
        groups = len(self.working_groups) + len(payload["new_groups"])
        decided_ops = []
        for index, item in enumerate(operations):
            path = f"operations[{index}]"
            if item["group"] is not None and not 1 <= item["group"] <= groups:
                raise _issue("reference", "unknown_group", _join(path, "group"),
                             "int (a supplied or new group number) or null", allowed=_numbers(range(1, groups + 1)))
            if item["exit"] in ("external_handoff", "unknown") and item["exit_question"] is None:
                raise _issue("contract", "exit_without_question", _join(path, "exit_question"),
                             "str naming the unknown receiver or continuation", actual="null")
            if item["initiation"] == "user_interaction" and (item["initiation_basis"] is None or not item["refs"]):
                raise _issue("contract", "interaction_without_source", _join(path, "initiation"),
                             "user_interaction only with initiation_basis and refs", actual="string")
            decided_ops.append({**{key: item[key] for key in ("name", "description", "actor", "kind", "initiation",
                                                             "initiation_basis", "group", "exit", "exit_question")},
                                "model_evidence": run.cite(item["refs"], staged, _join(path, "refs")),
                                "details": self.check_details(item.get("details"), _join(path, "details"), staged)})
        for index, item in enumerate(payload["new_groups"], len(self.working_groups) + 1):
            if not any(op["group"] == index for op in decided_ops):
                raise _issue("contract", "unused_group", f"new_groups[{index - len(self.working_groups) - 1}]",
                             "a new group that at least one new operation of this reply uses", actual="object")
        accounting, usage = {}, Counter()
        for index, item in enumerate(payload["unit_accounting"]):
            path = f"unit_accounting[{index}]"
            number = item["unit"]
            if not 1 <= number <= supplied:
                raise _issue("reference", "unknown_unit", _join(path, "unit"), "int (supplied unit number)",
                             allowed=_numbers(range(1, supplied + 1)))
            if number in accounting:
                raise _issue("contract", "duplicate_unit", _join(path, "unit"), "int (each unit number once)")
            for key, limit, code in (("new_operations", new_count, "unknown_new_operation"),
                                     ("known_operations", known_count, "unknown_known_operation")):
                values = item[key]
                for position, value in enumerate(values):
                    if not 1 <= value <= limit:
                        raise _issue("reference", code, f"{path}.{key}[{position}]", "int (supplied number)",
                                     allowed=_numbers(range(1, limit + 1)))
                    if value in values[:position]:
                        raise _issue("contract", "duplicate_values", f"{path}.{key}[{position}]", "unique numbers")
            for position, value in enumerate(item["serves"]):
                if not 1 <= value <= supplied or value == number:
                    raise _issue("reference", "unknown_unit", f"{path}.serves[{position}]",
                                 "int (another supplied unit number)",
                                 allowed=_numbers(other for other in range(1, supplied + 1) if other != number))
                if value in item["serves"][:position]:
                    raise _issue("contract", "duplicate_values", f"{path}.serves[{position}]", "unique numbers")
            links = [("new", value) for value in item["new_operations"]] + [("known", value)
                                                                         for value in item["known_operations"]]
            disposition = item["disposition"]
            if disposition == "realized" and not links:
                raise _issue("contract", "missing_operation_link", _join(path, "new_operations"),
                             "[int] with at least one operation in new_operations or known_operations", actual="array")
            plain = not links and item["exclusion"] is None
            valid = {"realized": item["exclusion"] is None and not item["serves"],
                     "supporting": plain and item["reason"] is not None,
                     "excluded": not links and not item["serves"] and item["exclusion"] is not None
                     and item["reason"] is not None,
                     "outside_process": plain and not item["serves"] and item["reason"] is not None,
                     "unresolved": plain and not item["serves"] and item["question"] is not None}[disposition]
            if not valid:
                raise _issue("contract", "disposition_fields", path, "fields matching the disposition", actual="object")
            accounting[number] = item
            usage.update(links)
        missing = sorted(set(range(1, supplied + 1)) - set(accounting))
        if missing:
            raise _issue("contract", "missing_units", "unit_accounting",
                         "one unit_accounting item per supplied unit number", actual="array",
                         missing_values=_numbers(missing))
        for index, item in enumerate(payload["unit_accounting"]):
            links = [("new", value) for value in item["new_operations"]] + [("known", value)
                                                                         for value in item["known_operations"]]
            if item["disposition"] == "realized" and item["reason"] is None and (
                    len(links) > 1 or item["known_operations"] or any(usage[link] > 1 for link in links)):
                raise _issue("contract", "missing_reason", f"unit_accounting[{index}].reason",
                             "str explaining the merge, split or reuse", actual="null")
        for position in range(1, new_count + 1):
            if not usage[("new", position)]:  # an action without supplied code is never proposed
                raise _issue("contract", "operation_without_unit", f"operations[{position - 1}]",
                             "an operation that at least one supplied unit realizes (new_operations)", actual="object")
        self.check_detail_sources(page, decided_ops, accounting, staged)
        mappings = []
        for index, item in enumerate(payload["event_mappings"]):
            path = f"event_mappings[{index}]"
            if not 1 <= item["site"] <= site_count:
                raise _issue("reference", "unknown_site", _join(path, "site"), "int (supplied site number)",
                             allowed=_numbers(range(1, site_count + 1)))
            site = page.sites[item["site"] - 1]
            if type(site.get("event_key")) is not str or site.get("key_status") not in _KEYED:
                raise _issue("contract", "site_without_key", _join(path, "site"), "int (site with an event key)")
            target = self.pick(item, "new_operation", "known_operation", path, new_count, known_count)
            if item["lifecycle_role"] != "unknown" and (item["role_basis"] is None or not item["refs"]):
                raise _issue("contract", "role_without_basis", _join(path, "role_basis"),
                             "str with refs for every lifecycle_role other than unknown",
                             actual="null" if item["role_basis"] is None else "string")
            mappings.append({"site": site, "target": target,
                             "evidence": run.cite(item["refs"], staged, _join(path, "refs")),
                             **{key: item[key] for key in ("lifecycle_role", "role_basis", "occurrence_rule",
                                                           "ambiguity")}})
        edges = []
        for index, item in enumerate(payload["operation_transitions"]):
            path = f"operation_transitions[{index}]"
            source = self.pick(item, "from_new", "from_known", path, new_count, known_count)
            target = self.pick(item, "to_new", "to_known", path, new_count, known_count)
            if source == target:
                raise _issue("contract", "self_transition", path, "a transition between two different operations",
                             actual="object")
            if not item["refs"]:  # every transition rests on supplied code; generated text is never a basis
                raise _issue("contract", "transition_basis", _join(path, "refs"), "[REF] with at least one item",
                             actual="array")
            evidence = run.cite(item["refs"], staged, _join(path, "refs"))
            if item["basis"] == "source_handoff":
                # A queue, topic, registry or callback association needs the same literal contract shown on both
                # sides: at least two distinct cited ranges whose frozen text contains it.
                contract = item["contract"]
                shown = sum(type(contract) is str and bool(contract.strip())
                            and contract in (staged[key].safe_quote or "") for key in evidence)
                if shown < 2:
                    raise _issue("contract", "handoff_without_contract", _join(path, "contract"),
                                 "str: the literal queue, topic, registry key or callback name that at least two "
                                 "distinct cited ranges show", actual="null" if contract is None else "string")
            edges.append({"from": source, "to": target, "condition": item["condition"], "basis": item["basis"],
                          "contract": item["contract"], "basis_note": item["basis_note"], "evidence": evidence})
        # Structural support from this page's own source; an unsupported proposal is demoted, never a page failure.
        for edge, found in zip(edges, self.supports(page, accounting, edges, staged)):
            edge.update(found)
        return {"operations": decided_ops, "new_groups": [dict(item) for item in payload["new_groups"]],
                "accounting": accounting, "mappings": mappings, "edges": edges,
                "transition_reviews": self.check_transition_reviews(page, payload.get("transition_review"), staged),
                "unknowns": list(payload["unknowns"]), "staged": staged}

    def check_transition_reviews(self, page, review, staged):
        if review is None:
            return []
        result, seen = [], {item["transition_id"] for item in self.transition_reviews}
        prior = {item["id"]: item for item in self.transition_proposals}
        for index, item in enumerate(review["withdrawals"]):
            path = f"transition_review.withdrawals[{index}]"
            key = item["transition_id"]
            if key not in prior or key in seen or not item["reason"].strip() or not item["refs"]:
                raise _issue("reference", "source_invalid_transition_review", path,
                             "one not-yet-withdrawn existing transition ID, a reason and current supplied source refs")
            seen.add(key)
            result.append({"transition_id": key, "reason": item["reason"],
                           "evidence_ids": self.run.cite(item["refs"], staged, path + ".refs")})
        return result

    def effective_proposals(self):
        withdrawn = {item["transition_id"] for item in self.transition_reviews}
        return [{**item, "state": "inactive", "reason": "source_review_withdrawal"} if item["id"] in withdrawn
                else item for item in self.transition_proposals]

    # Optional details of one new action: structure and citations here, its own-source boundary after accounting.

    def check_details(self, value, path, staged):
        """Citations and grounding of one action's optional details; never semantic or runtime proof.

        Returns None (omitted or null) or {"record": OperationDetails as a dict with host evidence IDs,
        "cited": [(field path, (path, first line, last line))]} for the boundary check of check_detail_sources.
        """
        if value is None:
            return None
        run, cited = self.run, []

        def cite(refs, where):
            ids = []
            for position, ref in enumerate(refs):
                item = run.cite_one(ref, staged, f"{where}[{position}]")
                anchor = item.source_anchor
                cited.append((f"{where}[{position}]", (anchor.path, anchor.start_line, anchor.end_line)))
                ids.append(item.id)
            return _unique(ids)
        claims = (any(value[key] is not None for key in _DETAIL_TEXTS) or value["effect_kind"] != "unknown"
                  or bool(value["outcomes"]) or value["continuation"] != "unknown" or value["join"] != "unknown")
        if claims and not value["refs"]:
            raise _issue("contract", "details_without_refs", _join(path, "refs"),
                         "[REF] with at least one item that shows every stated field", actual="array")
        evidence = cite(value["refs"], _join(path, "refs"))
        interactions = []
        for index, row in enumerate(value["external_interactions"]):
            where = f"{path}.external_interactions[{index}]"
            if not row["refs"]:
                raise _issue("contract", "external_without_refs", _join(where, "refs"),
                             "[REF] with at least one item: the cited call, message or callback", actual="array")
            ids = cite(row["refs"], _join(where, "refs"))
            if row["endpoint"] is None and row["contract"] is None:
                raise _issue("contract", "external_without_contract", where, "object with endpoint or contract",
                             actual="object")
            interactions.append(_build(where, ExternalInteraction, system=row["system"], direction=row["direction"],
                                       purpose=row.get("purpose"), endpoint=row["endpoint"],
                                       contract=row["contract"], evidence_ids=ids))
        explanations = []
        for index, row in enumerate(value["code_explanations"]):
            where = f"{path}.code_explanations[{index}]"
            if not row["refs"]:
                raise _issue("contract", "explanation_without_refs", _join(where, "refs"),
                             "[REF] with at least one item: the explained lines", actual="array")
            ids = cite(row["refs"], _join(where, "refs"))
            # A token in the frozen, redacted cited text; no declaration binding is claimed (e.g. Java overloads).
            if not _symbol_in(row["symbol"], "\n".join(staged[key].safe_quote or "" for key in ids)):
                raise _issue("contract", "explanation_symbol_not_in_source", _join(where, "symbol"),
                             "str (a function, method, class or field that occurs in the cited lines)",
                             actual="string")
            explanations.append(_build(where, CodeExplanation, symbol=row["symbol"], explanation=row["explanation"],
                                       evidence_ids=ids))
        record = _build(path, OperationDetails, **{key: value[key] for key in _DETAIL_TEXTS},
                        effect_kind=value["effect_kind"], outcomes=list(value["outcomes"]),
                        continuation=value["continuation"], join=value["join"], questions=list(value["questions"]),
                        external_interactions=interactions, code_explanations=explanations, evidence_ids=evidence)
        return {"record": record.to_dict(), "cited": cited}

    def check_detail_sources(self, page, decided_ops, accounting, staged):
        """No borrowed evidence: every details citation lies in the action's own refs, the supplied ranges of the
        units that realize it or of the supporting units that serve them; shared context alone is no source."""
        if not any(op["details"] for op in decided_ops):
            return
        entries = {entry["number"]: entry for entry in page.entries}
        own = {position: [] for position in range(1, len(decided_ops) + 1)}
        for position, op in enumerate(decided_ops, 1):
            for evidence_id in op["model_evidence"]:
                anchor = staged[evidence_id].source_anchor
                own[position].append((anchor.path, anchor.start_line, anchor.end_line))
        for number, item in accounting.items():
            for value in item["new_operations"]:
                own[value].extend(entries[number]["spans"])
            if item["disposition"] == "supporting":
                for served in item["serves"]:
                    for value in accounting[served]["new_operations"]:
                        own[value].extend(entries[number]["spans"])
        for position, op in enumerate(decided_ops, 1):
            for where, (path, first, last) in (op["details"] or {}).get("cited", []):
                lines = {line for source, start, end in own[position] if source == path
                         for line in range(max(start, first), min(end, last) + 1)}
                if len(lines) != last - first + 1:
                    raise _issue("reference", "details_ref_outside_operation", where,
                                 "[REF] inside this action's own refs, realizing units or serving supporting units",
                                 actual="object")

    # Structural support of proposed transitions (transition_support.py): host data of the proposing page only.

    def owned(self, identifier, spans):
        """A unit's owned source on a page: its supplied ranges clipped to its own lines, merged and sorted. A finding
        ref or site of other code that was supplied with the unit is never its source."""
        unit = self.units[identifier]
        first, last = self.lines(unit)
        merged = []
        for path, start, end in sorted({(path, max(start, first), min(end, last)) for path, start, end in spans
                                        if path == unit["path"] and start <= last and first <= end}):
            if merged and start <= merged[-1][2] + 1:
                merged[-1][2] = max(merged[-1][2], end)
            else:
                merged.append([path, start, end])
        return [tuple(item) for item in merged]

    def links_between(self, supplied):
        """Control links between units this page supplied: {(source, target): origins, relation IDs, call lines}.

        Navigation (resolved same-file calls, catalog callee anchors, host declaration callees) and host static calls
        are host links; resolved code-model relations of a control kind are kept as model provenance. Data handoffs,
        branches and unresolved targets are never links.
        """
        found = {}

        def add(source, target, origin, relation_id, lines):
            if source == target or source not in supplied or target not in supplied:
                return
            link = found.setdefault((source, target), {"origins": set(), "relation_ids": set(), "lines": set()})
            link["origins"].add(origin)
            if relation_id is not None:
                link["relation_ids"].add(relation_id)
            link["lines"].update(lines)
        for source in supplied:
            unit = self.units[source]
            caller = unit.get("declaration_id")
            for target in [*unit["navigation"]["callee_unit_ids"], *self.host_callees.get(source, [])]:
                if target in self.units:
                    callee = self.units[target].get("declaration_id")
                    add(source, target, "navigation", None,
                        self.call_lines.get((caller, callee), []) if caller and callee else [])
            for relation, target in self.relations.get(source, ()):
                if target is None or relation["resolution"] != "resolved":
                    continue
                origin = relation["origin"]
                if origin == "host_static" or (origin == "worker_proposal" and relation["kind"] in CONTROL_KINDS):
                    where = relation["source"]
                    add(source, target, origin, relation["id"],
                        [(where["path"], where["start_line"], where["end_line"])])
        return found

    def supports(self, page, accounting, edges, staged):
        """{state, reason, support, endpoints} of each proposed transition of one page, in reply order.

        Endpoint units come from the validated accounting: a new action's realizing units and the supporting units
        serving them; a known action's earlier realizing units, the supporting units that served them and this page's
        units that realize it again. Deterministic and replayable; an unsupported claim is demoted, never raised.
        """
        if not edges:
            return []
        entries = {entry["number"]: entry for entry in page.entries}
        supplied = {}  # owned source only; include reads actually present in THIS semantic request
        for row in [*page.entries, *page.shared]:
            supplied.setdefault(row["uid"], []).extend(row["spans"])
        for uid, spans in getattr(page, "observation_units", {}).items():
            supplied.setdefault(uid, []).extend(spans)
        supplied = {uid: self.owned(uid, spans) for uid, spans in supplied.items()}
        supplied = {uid: spans for uid, spans in supplied.items() if spans}
        links = self.links_between(supplied)
        served = {}  # unit -> supporting units of accepted pages that serve it
        for identifier, state in self.states.items():
            if state.get("state") == "accounted" and state.get("disposition") == "supporting":
                for unit in state.get("serves_unit_ids") or []:
                    served.setdefault(unit, []).append(identifier)

        def side(link):
            kind, value = link
            key = "new_operations" if kind == "new" else "known_operations"
            numbers = [number for number, item in sorted(accounting.items()) if value in item[key]]
            earlier = list(page.known[value - 1]["unit_ids"]) if kind == "known" else []
            units = [*earlier, *(helper for unit in earlier for helper in served.get(unit, []))]
            units += [entries[number]["uid"] for number in numbers]
            units += [entries[number]["uid"] for number, item in sorted(accounting.items())
                      if item["disposition"] == "supporting" and set(item["serves"]) & set(numbers)]
            return list(dict.fromkeys(units))
        found = []
        for edge in edges:
            evidence = [(key, (staged[key].source_anchor.path, staged[key].source_anchor.start_line,
                               staged[key].source_anchor.end_line), staged[key].safe_quote) for key in edge["evidence"]]
            sending, receiving = side(edge["from"]), side(edge["to"])
            state, reason, support = classify(edge["basis"], edge["contract"], evidence, sending, receiving, supplied,
                                              links)
            found.append({"state": state, "reason": reason, "support": support,
                          "endpoints": {"from_unit_ids": sending, "to_unit_ids": receiving}})
        return found

    def open_control(self, entry):
        """Open control links of one page unit, each with its address: unresolved dispatch, callback or control
        relations (code-model proposals) and control-linked callees that this page did not supply."""
        uid = entry["uid"]
        unit = self.units[uid]
        found = []
        for relation, _ in self.relations.get(uid, ()):
            if (relation["origin"] == "worker_proposal" and relation["kind"] in OPEN_KINDS
                    and relation["resolution"] == "unresolved"):
                target, where = relation["target"], relation["source"]
                found.append({"kind": "unresolved_relation", "relation_id": relation["id"], "target_unit_id": None,
                              "target": target.get("label") or target.get("qualname") or relation["kind"],
                              "where": {"path": where["path"], "start_line": where["start_line"],
                                        "end_line": where["end_line"]}})
        control = {*unit["navigation"]["callee_unit_ids"], *self.host_callees.get(uid, []),
                   *(target for relation, target in self.relations.get(uid, ()) if target is not None
                     and (relation["origin"] == "host_static" or relation["kind"] in CONTROL_KINDS))}
        for target in entry.get("omitted_ids", []):
            if target in control and target in self.units:
                first, last = self.lines(self.units[target])
                found.append({"kind": "dependency_not_supplied", "relation_id": None, "target_unit_id": target,
                              "target": _symbol(self.units[target]),
                              "where": {"path": self.units[target]["path"], "start_line": first, "end_line": last}})
        return found

    # Commit one accepted page: host IDs, host evidence, unit states and source-bounded event support.

    def host_evidence(self, ref, staged):
        item = self.snapshot.evidence(ref, max_lines=MAX_CITATION_LINES)
        known = staged.get(item.id) or self.staged.get(item.id) or self.run.evidence.get(item.id)
        if known is not None and known != item:
            raise ValueError("conflicting evidence for one registry ID")
        staged[item.id] = item
        return item.id

    def commit(self, page, decided, request_id):
        staged = dict(decided["staged"])
        new_ids = [stable_id("aop", page.key, index) for index in range(1, len(decided["operations"]) + 1)]
        groups = [{"id": stable_id("awg", page.key, index), "name": item["name"], "contract": item["contract"],
                   "page": page.number, "request_id": request_id}
                  for index, item in enumerate(decided["new_groups"], 1)]
        group_ids = [group["id"] for group in [*self.working_groups, *groups]]

        def resolve(link):
            return new_ids[link[1] - 1] if link[0] == "new" else page.known[link[1] - 1]["id"]
        # The stage of an operation is not known on a page: it is derived after all pages (finish).
        operations = [{"id": new_ids[index], "stage_id": None, "request_id": request_id,
                       "page": page.number, "position": index + 1,
                       **{key: item[key] for key in ("name", "description", "actor", "kind", "initiation",
                                                     "initiation_basis", "exit", "exit_question")},
                       "group_id": None if item["group"] is None else group_ids[item["group"] - 1],
                       "unit_ids": [], "model_evidence_ids": list(item["model_evidence"]), "unit_evidence_ids": [],
                       "details": None if item["details"] is None else item["details"]["record"]}
                      for index, item in enumerate(decided["operations"])]
        earlier = {op["id"]: {**op, "unit_ids": list(op["unit_ids"]), "unit_evidence_ids": list(op["unit_evidence_ids"])}
                   for op in self.ops}
        by_id = {**earlier, **{op["id"]: op for op in operations}}
        sources = {}
        for op in operations:
            for evidence_id in op["model_evidence_ids"]:
                anchor = staged[evidence_id].source_anchor
                sources.setdefault(op["id"], []).append((anchor.path, anchor.start_line, anchor.end_line))
        numbers = {entry["number"]: entry for entry in page.entries}
        states, unknowns = {}, []
        for number in sorted(decided["accounting"]):
            item = decided["accounting"][number]
            entry = numbers[number]
            unit = self.units[entry["uid"]]
            targets = [resolve(("new", value)) for value in item["new_operations"]]
            targets += [resolve(("known", value)) for value in item["known_operations"]]
            states[entry["uid"]] = {
                "state": "accounted", "request_id": request_id, "disposition": item["disposition"],
                "operation_ids": targets, "stage_ids": [],  # set from the derived stages at finish
                "serves_unit_ids": [numbers[value]["uid"] for value in item["serves"]],
                "exclusion": item["exclusion"], "reason": item["reason"], "question": item["question"],
                "role": entry["role"], "declaration_supplied": "whole" if entry["whole"] else "cited_ranges_only"}
            if item["disposition"] == "unresolved":
                unknowns.append(f"Единица {_symbol(unit)} ({unit['path']}:{self.lines(unit)[0]}-{self.lines(unit)[1]}): "
                                f"{item['question']}")
            if not targets:
                continue
            # The unit's own finding ranges become evidence of its operations: host-cited from ranges this request
            # supplied, recorded separately from the model's own refs.
            evidence = [self.host_evidence({key: ref[key] for key in ("path", "start_line", "end_line", "symbol")
                                            if key in ref}, staged)
                        for finding in self.findings(unit) for ref in finding.get("refs", [])
                        if type(ref) is dict and covering_ranges(page.ranges, ref.get("path"), ref.get("start_line"),
                                                                 ref.get("end_line")) is not None]
            if unit.get("origin") == ORIGIN:
                # No finding: the realizing declaration's opening lines, supplied on this page, are linked as its
                # structural anchor (navigation, semantically unverified); effects rest on the model's own refs.
                first, last = _anchor_lines(*self.lines(unit))
                head = {"path": unit["path"], "start_line": first, "end_line": last, "symbol": _symbol(unit)}
                if covering_ranges(page.ranges, head["path"], head["start_line"], head["end_line"]) is not None:
                    evidence.append(self.host_evidence(head, staged))
            spans = list(entry["spans"])  # visible shared context is not an implicit operation source
            for target in targets:
                holder = by_id[target]
                holder["unit_ids"] = _unique([*holder["unit_ids"], entry["uid"]])
                holder["unit_evidence_ids"] = _unique([*holder["unit_evidence_ids"], *evidence])
                sources.setdefault(target, []).extend(spans)
        # Supporting code adds its supplied ranges to the sources of the operations of the units it serves.
        for identifier, state in states.items():
            if state["disposition"] != "supporting":
                continue
            entry = next(item for item in page.entries if item["uid"] == identifier)
            for served in state["serves_unit_ids"]:
                for target in states.get(served, {}).get("operation_ids", []):
                    sources.setdefault(target, []).extend(entry["spans"])
        merged_sources = {key: [*self.op_sources.get(key, []), *value] for key, value in sources.items()}
        names = {op_id: op["name"] for op_id, op in by_id.items()}
        mappings, diagnostics = [], []
        for item in decided["mappings"]:
            site, operation_id = item["site"], resolve(item["target"])
            site_ref = {"path": site["path"], "start_line": site["start_line"], "end_line": site["end_line"],
                        "symbol": site.get("symbol") or "<module>"}
            if site.get("kind") in _NOT_STATIC:
                support = "no_static_emit"
            elif any(path == site["path"] and first <= site["start_line"] and site["end_line"] <= last
                     for path, first, last in merged_sources.get(operation_id, self.op_sources.get(operation_id, []))):
                support = "emit_within_operation_sources"
            else:
                support = "emit_outside_operation_sources"
            mapping = {"event_key": site["event_key"], "site_id": site["id"], "path": site["path"],
                       "start_line": site["start_line"], "end_line": site["end_line"], "operation_id": operation_id,
                       "operation_name": names[operation_id], "stage_id": None,  # the operation's stage, at finish
                       "lifecycle_role": item["lifecycle_role"], "role_basis": item["role_basis"],
                       "occurrence_rule": item["occurrence_rule"], "ambiguity": item["ambiguity"],
                       "role_support": support, "fact_ids": [], "request_id": request_id,  # facts linked at finish
                       "evidence_ids": _unique([self.host_evidence(site_ref, staged), *item["evidence"]])}
            mappings.append(mapping)
            for proposal in self.proposals.get(site["id"], []):
                for kind, differs in (("role_differs", proposal["lifecycle_role"] != mapping["lifecycle_role"]
                                       and "unknown" not in (proposal["lifecycle_role"], mapping["lifecycle_role"])),):
                    if differs:
                        diagnostics.append({"prior_index": proposal["prior_index"], "event_key": mapping["event_key"],
                                            "site_id": site["id"], "kind": kind,
                                            "prior": {key: proposal[key] for key in ("operation_label", "stage_id",
                                                                                     "lifecycle_role")},
                                            "current": {key: mapping[key] for key in ("operation_id", "stage_id",
                                                                                      "lifecycle_role", "request_id")}})
        # Every proposal is kept with its host state; only active ones join the operation graph.
        proposals = [{"id": stable_id("aot", page.key, index), "request_id": request_id, "page": page.number,
                      "from": resolve(item["from"]), "to": resolve(item["to"]), "condition": item["condition"],
                      "basis": item["basis"], "contract": item["contract"], "basis_note": item["basis_note"],
                      "evidence_ids": list(item["evidence"]), "state": item["state"], "reason": item["reason"],
                      "support": copy.deepcopy(item["support"]),
                      "endpoints": {"from_unit_ids": list(item["endpoints"]["from_unit_ids"]),
                                    "to_unit_ids": list(item["endpoints"]["to_unit_ids"])}}
                     for index, item in enumerate(decided["edges"], 1)]
        edges = [item for item in proposals if item["state"] == "active"]
        # Open control links of the units that realize an operation here, addressed for the frontier.
        links = [{"operation_id": target, "unit_id": entry["uid"], "request_id": request_id, "page": page.number,
                  **link}
                 for entry in page.entries if entry["uid"] in states
                 for link in self.open_control(entry) for target in states[entry["uid"]]["operation_ids"]]
        # Everything above is local; apply it only now, so a failed commit leaves no partial state.
        self.working_groups.extend(groups)
        self.staged.update(staged)
        self.ops = [earlier[op["id"]] for op in self.ops] + operations
        self.op_sources.update(merged_sources)
        for identifier, state in states.items():
            self.states[identifier].update(state)
        self.mappings.extend(mappings)
        self.record["prior_diagnostics"].extend(diagnostics)
        self.transition_proposals.extend(proposals)
        self.transition_reviews.extend({**item, "request_id": request_id, "page": page.number}
                                       for item in decided["transition_reviews"])
        withdrawn = {item["transition_id"] for item in self.transition_reviews}
        self.edges = [item for item in [*self.edges, *edges] if item["id"] not in withdrawn]
        self.open_links.extend(links)
        self.unknowns.extend([*unknowns, *decided["unknowns"]])

    # Optional triage.

    def estimate_pages(self, queue):
        """Offline page count of the current plan (no request): the measured need for triage."""
        pending, count = list(queue), 0
        while pending:
            planned = self.plan(pending, count + 1)
            if type(planned) is tuple:
                pending.remove(planned[1])
                continue
            count += 1
            taken = {entry["uid"] for entry in planned.entries}
            pending = [item for item in pending if item not in taken]
        return count

    def triage_page(self, rows, number):
        context = {"task": "map-triage", "business_hypothesis": self.business,
                   "units": [{"number": index, "symbol": _symbol(self.units[identifier]),
                              "path": self.units[identifier]["path"], "status": self.units[identifier]["status"],
                              "signals": self.signals(self.units[identifier]),
                              "findings": [{"kind": item["kind"], "description": item["description"]}
                                           for item in self.findings(self.units[identifier])]}
                             for index, identifier in enumerate(rows, 1)],
                   "page": {"triage_page": number, "units_supplied": len(rows)}}
        return _Page(context, [], number=number, rows=list(rows))

    def triage(self, queue):
        mode, record = self.settings.triage, self.record["triage"]
        if mode == "off":
            record["status"] = "off"
            return queue
        if mode == "auto":
            planned = self.estimate_pages(queue)
            record["planned_atomic_pages"] = planned
            if planned <= self.budget_left():
                record["status"] = "not_needed"
                return queue
        if self.budget_left() <= 1:  # a triage page would leave no request for any atomic page
            record["status"] = "skipped_request_limit"
            return queue
        # Triage sees findings only, never source: source work (a host declaration unit without findings, a context
        # unit whose findings are technical labels) is never offered and always goes to a page with its own code.
        pending = [identifier for identifier in queue if not self.source_work(self.units[identifier])]
        number, decided = 0, set()
        while pending and not self.halted and self.budget_left() > 1:  # keep room for at least one atomic page
            rows = []
            for identifier in pending[:TRIAGE_PAGE_UNITS]:
                if not self.within(TRIAGE_SYSTEM, self.triage_page(rows + [identifier], number + 1).context,
                                   single=not rows):
                    break
                rows.append(identifier)
            if not rows:
                break
            number += 1
            page = self.triage_page(rows, number)
            pending = pending[len(rows):]
            entry, result, _ = self.request("triage", TRIAGE_SYSTEM, page, TRIAGE_SHAPE, self.validate_triage,
                                            {"unit_ids": list(rows)})
            record["request_ids"].append(entry["id"])
            if result is None:
                continue  # the units keep their place in the atomic queue
            for position, item in result.items():
                identifier = rows[position - 1]
                if item["decision"] == "atomic_review":
                    continue
                decided.add(identifier)
                self.states[identifier].update({**_state("triaged", item["reason"], entry["id"]),
                                                "disposition": item["decision"], "question": item["question"]})
                if item["decision"] == "unresolved":
                    unit = self.units[identifier]
                    self.unknowns.append(f"Единица {_symbol(unit)} ({unit['path']}): {item['question']}")
        record["status"] = "complete" if not pending else "partial"
        record["units_triaged_out"] = len(decided)
        return [identifier for identifier in queue if identifier not in decided]

    def validate_triage(self, page, payload):
        supplied, decided = len(page.rows), {}
        for index, item in enumerate(payload["units"]):
            path = f"units[{index}]"
            if not 1 <= item["unit"] <= supplied:
                raise _issue("reference", "unknown_unit", _join(path, "unit"), "int (supplied unit number)",
                             allowed=_numbers(range(1, supplied + 1)))
            if item["unit"] in decided:
                raise _issue("contract", "duplicate_unit", _join(path, "unit"), "int (each unit number once)")
            decision = item["decision"]
            if ((decision in ("supporting", "outside_process") and item["reason"] is None)
                    or (decision == "unresolved" and item["question"] is None)):
                raise _issue("contract", "decision_fields", path, "fields matching the decision", actual="object")
            decided[item["unit"]] = item
        missing = sorted(set(range(1, supplied + 1)) - set(decided))
        if missing:
            raise _issue("contract", "missing_units", "units", "one units item per supplied unit number",
                         actual="array", missing_values=_numbers(missing))
        return decided

    # Optional composition of display scenarios.

    def compose(self):
        """The one composition request after all pages: stages, boundary facts, obligation and hypothesis checks.

        Nothing is applied here; finish derives the map from the validated reply, or keeps the provisional groups
        and host boundary facts when this request is skipped or rejected.
        """
        record = self.record["compose"]
        if not self.ops:
            record["status"] = "skipped_no_operations"
            return
        if self.halted or self.budget_left() <= 0:
            record["status"] = "skipped_" + (self.halted or "request_limit_reached")
            return
        numbers = {op["id"]: index for index, op in enumerate(self.ops, 1)}
        context = {"task": "map-compose",
                   "working_groups": [{"number": index, "name": group["name"], "contract": group["contract"]}
                                      for index, group in enumerate(self.working_groups, 1)],
                   "operations": [{"number": index, "name": op["name"], "group": self.group_number(op["group_id"]),
                                   "kind": op["kind"], "initiation": op["initiation"], "exit": op["exit"],
                                   "description": op["description"],
                                   "units": [_symbol(self.units[uid]) for uid in op["unit_ids"] if uid in self.units],
                                   # accepted exact-site records of this action: what a fact's lifecycle roles can see
                                   "events": [dict(item) for item in dict.fromkeys(
                                       (("event_key", mapping["event_key"]), ("lifecycle_role", mapping["lifecycle_role"]))
                                       for mapping in self.mappings if mapping["operation_id"] == op["id"])]}
                                  for index, op in enumerate(self.ops, 1)],
                   # Active transitions only; unsupported proposals are listed apart, without a condition, as no order.
                   "transitions": [{"from": numbers[edge["from"]], "to": numbers[edge["to"]],
                                    "condition": edge["condition"], "basis": edge["basis"]} for edge in self.edges],
                   "unsupported_transitions": [{"from": numbers[item["from"]], "to": numbers[item["to"]],
                                                "basis": item["basis"], "reason": item["reason"]}
                                               for item in self.effective_proposals() if item["state"] == "inactive"],
                   "approved_obligations": [{"number": index, "text": item["text"]}
                                            for index, item in enumerate((self.obligations or {}).get("items", []), 1)],
                   "scenarios_requested": bool(self.settings.compose),
                   "business_hypothesis": self.business}
        page = _Page(context, [], number=1)
        if not self.within(COMPOSE_SYSTEM, context, single=True):
            record["status"] = "skipped_exceeds_request_budget"
            return
        entry, result, _ = self.request("compose", COMPOSE_SYSTEM, page, COMPOSE_SHAPE, self.validate_compose,
                                        {"operation_ids": [op["id"] for op in self.ops]})
        record["request_id"] = entry["id"]
        if result is None:
            record["status"] = "failed"
            return
        self.composed = {"key": page.key, "request_id": entry["id"], **result}
        self.unknowns.extend(result["payload"]["unknowns"])
        omitted = (entry.get("guidance_revalidation") or {}).get("warnings", [])
        if omitted:
            self.unknowns.append(f"Сборка этапов: после исправления ссылки фактов на методику не прошли проверку и "
                                 f"опущены ({len(omitted)}); атрибуция этих фактов не установлена "
                                 "(atomic_map.json, guidance_revalidation).")
        record["status"] = "complete"

    def validate_compose(self, page, payload):
        """Every accepted operation in exactly one stage; facts at operation boundaries of their stage; one
        status per supplied obligation; known numbers only. Stage order is never taken from this reply."""
        count, stages = len(self.ops), len(payload["stages"])
        obligations = len(page.context["approved_obligations"])

        def operation_numbers(values, path):
            for position, value in enumerate(values):
                if not 1 <= value <= count:
                    raise _issue("reference", "unknown_operation_number", f"{path}[{position}]",
                                 "int (supplied operation number)", allowed=_numbers(range(1, count + 1)))
                if value in values[:position]:
                    raise _issue("contract", "duplicate_values", f"{path}[{position}]", "unique numbers")
        stage_of = {}
        for index, item in enumerate(payload["stages"]):
            path = f"stages[{index}]"
            if not item["operations"]:
                raise _issue("contract", "empty_stage", _join(path, "operations"), "[int] with at least one item",
                             actual="array")
            operation_numbers(item["operations"], _join(path, "operations"))
            for position, value in enumerate(item["operations"]):
                if value in stage_of:
                    raise _issue("contract", "operation_in_two_stages", f"{path}.operations[{position}]",
                                 "int (an operation not placed in another stage)")
                stage_of[value] = index + 1
        missing = sorted(set(range(1, count + 1)) - set(stage_of))
        if missing:
            raise _issue("contract", "missing_operations", "stages", "every operation number in exactly one stage",
                         actual="array", missing_values=_numbers(missing))
        statuses = {}
        for index, item in enumerate(payload["obligations"]):
            path = f"obligations[{index}]"
            if not 1 <= item["obligation"] <= obligations:
                raise _issue("reference", "unknown_obligation", _join(path, "obligation"),
                             "int (supplied obligation number)", allowed=_numbers(range(1, obligations + 1)))
            if item["obligation"] in statuses:
                raise _issue("contract", "duplicate_obligation", _join(path, "obligation"), "each obligation once")
            operation_numbers(item["operations"], _join(path, "operations"))
            if ((item["status"] == "not_found") == bool(item["operations"])
                    or (item["status"] != "matched" and item["question"] is None)):
                raise _issue("contract", "obligation_fields", path, "operations for matched or partial, none for "
                             "not_found; a question unless matched", actual="object")
            statuses[item["obligation"]] = item
        missing = sorted(set(range(1, obligations + 1)) - set(statuses))
        if missing:
            raise _issue("contract", "missing_obligations", "obligations", "one item per supplied obligation",
                         actual="array", missing_values=_numbers(missing))
        guidance = []
        for index, item in enumerate(payload["facts"]):
            path = f"facts[{index}]"
            operation_numbers(item["operations"], _join(path, "operations"))
            if item["obligation"] is not None and not 1 <= item["obligation"] <= obligations:
                raise _issue("reference", "unknown_obligation", _join(path, "obligation"),
                             "int (supplied obligation number) or null", allowed=_numbers(range(1, obligations + 1)))
            if item["operations"]:
                if item["stage"] is None or not 1 <= item["stage"] <= stages:
                    raise _issue("reference", "unknown_stage_number", _join(path, "stage"),
                                 "int (a stage number of this reply)", allowed=_numbers(range(1, stages + 1)))
                for position, value in enumerate(item["operations"]):
                    if stage_of[value] != item["stage"]:
                        raise _issue("contract", "fact_stage_mismatch", f"{path}.operations[{position}]",
                                     "int (an operation of the fact's stage)")
            elif (item["obligation"] is None or item["stage"] is not None
                  or statuses[item["obligation"]]["status"] == "matched"):
                # A fact without operations only observes an approved obligation that no accepted action realizes.
                raise _issue("contract", "fact_without_operations", _join(path, "operations"),
                             "[int] of accepted operations; [] only with an unmatched approved obligation and stage "
                             "null", actual="array")
            guidance.append(self.fact_guidance(item, path))
        for index, item in enumerate(payload["hypotheses"]):
            operation_numbers(item["operations"], f"hypotheses[{index}].operations")
        if payload["scenarios"] and not page.context["scenarios_requested"]:
            raise _issue("contract", "scenarios_not_requested", "scenarios", "[] (scenarios_requested is false)",
                         actual="array")
        for index, item in enumerate(payload["scenarios"]):
            path = f"scenarios[{index}]"
            if not item["operations"]:
                raise _issue("contract", "empty_scenario", _join(path, "operations"),
                             "[int] with at least one item", actual="array")
            operation_numbers(item["operations"], _join(path, "operations"))
            if item["obligation"] is not None and not 1 <= item["obligation"] <= obligations:
                raise _issue("reference", "unknown_obligation", _join(path, "obligation"),
                             "int (supplied obligation number) or null", allowed=_numbers(range(1, obligations + 1)))
        for index, item in enumerate(payload["groups"]):
            path = f"groups[{index}]"
            if not 1 <= item["stage"] <= stages:
                raise _issue("reference", "unknown_stage_number", _join(path, "stage"), "int (a stage number of this "
                             "reply)", allowed=_numbers(range(1, stages + 1)))
            operation_numbers(item["operations"], _join(path, "operations"))
            for position, value in enumerate(item["operations"]):
                if stage_of[value] != item["stage"]:
                    raise _issue("contract", "group_stage_mismatch", f"{path}.operations[{position}]",
                                 "int (an operation of the group's stage)")
        return {"payload": payload, "stage_of": stage_of, "statuses": statuses, "guidance": guidance}

    # Assembly.

    def status_lines(self):
        lines, deferred, failed = [], Counter(), Counter()
        for state in self.states.values():
            if state["state"] == "deferred":
                deferred[state["reason"]] += 1
            elif state["state"] == "request_failed":
                failed[state["reason"]] += 1
        for reason, count in sorted(deferred.items()):
            lines.append(f"Атомарные действия: {count} единиц команд отложено ({_REASONS_RU.get(reason, reason)}); "
                         "список — atomic_map.json.")
        for reason, count in sorted(failed.items()):
            lines.append(f"Атомарные действия: по {count} единицам команд решение не получено (запрос не принят: "
                         f"{reason}); единицы сохранены в atomic_map.json.")
        for item in self.mappings:
            if item["role_support"] != "emit_within_operation_sources":
                lines.append(f"Событие {item['event_key']} ({item['path']}:{item['start_line']}): место вызова не "
                             f"входит в источники операции «{item['operation_name']}»; привязка не подтверждена.")
            if item["ambiguity"]:
                lines.append(f"Событие {item['event_key']} ({item['path']}:{item['start_line']}): {item['ambiguity']}")
        for item in self.record["requests"]:
            local = item.get("local_revalidation")
            if local is not None and local["outcome"] == "accepted" and item["outcome"] != "commit_failed":
                sites = sum(omission["field"] == SITE_FIELD for omission in local["omissions"])
                lines.append(f"Атомарная страница {item['page']}: ответ после исправления не прошёл проверку только из-за "
                             "пунктов, которые можно безопасно исключить (деталей операций — "
                             f"{len(local['omissions']) - sites}, мест логирования без операции и без фактов — {sites}); "
                             "они исключены локально, остальное принято после полной повторной проверки без нового "
                             "запроса к модели. Ответ как получен остаётся отклонённым, исключённые места остаются без "
                             "этой привязки; исключения — в atomic_map.json.")
        binding = self.record.get("declaration_coverage")
        if binding is not None:
            # Found is not sent: only units of attempted page requests with an API request hash (failed ones included)
            # are counted; this is not proof of wire delivery; checkpoint reuse sends nothing; deferred and failed units
            # stay counted apart. Context units are counted on their own line: they do have technical findings.
            atomic = [item for item in self.record["requests"] if item["kind"] == "atomic"]
            sent = {uid for item in atomic if any(attempt["attempt"] >= 1 and attempt.get("api_request_sha256")
                                                  for attempt in item["attempts"]) for uid in item["unit_ids"]}
            reused = {uid for item in atomic if item["outcome"] == "accepted_from_checkpoint"
                      and "source_observation" not in item for uid in item["unit_ids"]}
            for origin, title in ((ORIGIN, "Объявления без находок технического разбора"),
                                  (CONTEXT_ORIGIN, "Объявления, которые технический разбор отметил только как "
                                                   "контекст, а не как кандидаты действий")):
                hosts = {key for key, state in self.states.items() if state.get("origin") == origin}
                if not hosts:
                    continue
                states = Counter(self.states[key]["state"] for key in hosts)
                lines.append(f"{title}: найдено {len(hosts)}; в попытках модельных "
                             f"запросов страниц (включая неудачные) — {len(sent & hosts)}; из сохранённых ответов — "
                             f"{len(reused & hosts)}; решение получено — {states['accounted']}; отложено — "
                             f"{states['deferred']}; без решения из-за ошибки запроса — {states['request_failed']}. "
                             "Перечень — declaration_coverage.json, состояние каждого — atomic_map.json.")
            if binding["open_scope"]["blocking"]:
                lines.append(f"Перечень объявлений неизвестен для {binding['open_scope']['blocking']} файлов исходного "
                             "кода (язык без индекса объявлений, неполный или отклонённый разбор): их код без находок "
                             "не проверен, карта не считается полной; список — declaration_coverage.json.")
        for op in self.ops:  # what the page could not settle about one action stays an addressed question
            for question in (op.get("details") or {}).get("questions", []):
                lines.append(f"Операция «{op['name']}»: {question}")
        led = self.record["code_led"]
        compose = self.record["compose"]["status"]
        if self.ops and compose != "complete":
            lines.append(f"Этапы не выведены из принятых действий (сборка: {compose}); показаны рабочие группы страниц "
                         "и граничные факты хоста, карта частичная.")
        for gap in led["gaps"]:  # addressed: the gap ID names its operations and units in atomic_map.json
            lines.append(f"Разрыв {gap['id']} ({gap['kind']}): {gap['question']}")
        for item in (led["obligations"] or {}).get("items", []):
            if item["status"] != "matched":
                state = {"not_found": "не найдено в принятых действиях", "partial": "реализовано частично",
                         "not_checked": "не сопоставлено"}[item["status"]]
                lines.append(f"Утверждённое требование {item['id']} «{item['text']}»: {state}"
                             + (f"; {item['question']}" if item.get("question") else "") + ".")
        for item in led["hypotheses"]:
            if item["status"] in ("contradicted", "not_found"):
                lines.append(f"Гипотеза из бизнес-описания ({item['status']}): {item['statement']}"
                             + (f" — {item['note']}" if item["note"] else ""))
        if len(led["frontier"]["fragments"]) > 1:
            lines.append(f"Карта состоит из {len(led['frontier']['fragments'])} несвязанных фрагментов; связи между "
                         "ними кодом не показаны и не добавлялись (фрагменты — atomic_map.json).")
        return lines

    # Derivation after all pages: stages, stage order, facts, obligations, hypotheses and the frontier.

    def derive_stages(self):
        """(stage items, operation -> stage ID): from the composition, else the working groups and one ungrouped stage.

        Stage IDs come from the request or the group they derive from; nothing here orders stages.
        """
        items = []
        if self.composed is not None:
            for index, item in enumerate(self.composed["payload"]["stages"], 1):
                items.append({"id": stable_id("ast", self.composed["key"], index), "name": item["name"],
                              "contract": item["contract"], "origin": "compose", "group_id": None,
                              "operation_ids": [self.ops[number - 1]["id"] for number in item["operations"]]})
        elif self.ops:
            for group in self.working_groups:
                members = [op["id"] for op in self.ops if op["group_id"] == group["id"]]
                if members:
                    items.append({"id": stable_id("ast", "working_group", group["id"]), "name": group["name"],
                                  "contract": group["contract"], "origin": "working_group", "group_id": group["id"],
                                  "operation_ids": members})
            loose = [op["id"] for op in self.ops if op["group_id"] is None]
            if loose:
                items.append({"id": stable_id("ast", UNGROUPED, self.units_doc["units_sha256"]),
                              "name": "Действия без группы",
                              "contract": "Принятые действия, для которых группа не установлена; это не бизнес-этап.",
                              "origin": UNGROUPED, "group_id": None, "operation_ids": loose})
        stage_of = {key: item["id"] for item in items for key in item["operation_ids"]}
        return items, stage_of

    def stage_transitions(self, stage_of):
        """Stage order derived only from accepted operation transitions that cross two stages."""
        rows = {}
        for edge in self.edges:
            source, target = stage_of[edge["from"]], stage_of[edge["to"]]
            if source == target:
                continue
            row = rows.setdefault((source, target), {"from_stage": source, "to_stage": target, "transition_ids": [],
                                                      "evidence_ids": [], "conditions": []})
            row["transition_ids"].append(edge["id"])
            row["evidence_ids"] = _unique([*row["evidence_ids"], *edge["evidence_ids"]])
            if edge["condition"] is not None:
                row["conditions"] = _unique([*row["conditions"], edge["condition"]])
        return list(rows.values())

    def pending_callees(self, op):
        """Local units an operation's code calls whose own decision is still missing: pending work, not external."""
        found = []
        for identifier in op["unit_ids"]:
            kept, omitted = self.dependencies(self.units[identifier])
            found += [item for item in kept + omitted if item in self.states
                      and self.states[item]["state"] in ("deferred", "request_failed")]
        return list(dict.fromkeys(found))

    def frontier(self):
        """Entries, terminals, fragments and addressed gaps, computed from accepted operations and transitions only.

        Several independent entries are allowed. No edge is created: an open exit stays a gap with its question.
        """
        ops = {op["id"]: op for op in self.ops}
        incoming, outgoing, neighbours = Counter(), Counter(), {key: set() for key in ops}
        for edge in self.edges:
            outgoing[edge["from"]] += 1
            incoming[edge["to"]] += 1
            neighbours[edge["from"]].add(edge["to"])
            neighbours[edge["to"]].add(edge["from"])
        gaps = []

        def gap(kind, operation_ids, unit_ids, question, source):
            gaps.append({"id": gap_id(kind, operation_ids, unit_ids), "kind": kind,
                         "operation_ids": list(operation_ids), "unit_ids": list(unit_ids), "question": question,
                         "source": source})
        opened = still_open(self.open_links, ops, self.states, self.edges)
        for key, op in ops.items():
            pending = self.pending_callees(op)
            if pending:
                names = ", ".join(_symbol(self.units[item]) for item in pending)
                gap("pending_local_work", [key], pending, f"Вызываемый локальный код ({names}) ещё не разобран: "
                    f"продолжение операции «{op['name']}» не установлено до его решения.", "host")
            if op["exit"] == "continues" and not outgoing[key] and not pending:
                gap("open_continuation", [key], op["unit_ids"], f"Операция «{op['name']}» продолжается в коде, но "
                    "следующий шаг среди принятых операций не установлен.", "host")
            elif op["exit"] == "external_handoff":
                gap("unresolved_handoff", [key], op["unit_ids"], op["exit_question"], "model")
            elif op["exit"] == "unknown":
                gap("unknown_continuation", [key], op["unit_ids"], op["exit_question"], "model")
            if key in opened:  # whatever exit the model claimed: a local open link is neither external nor an end
                rows = opened[key]
                why = {"unresolved_relation": "связь предложена моделью кода, получатель не разрешён",
                       "dependency_not_supplied": "код не был передан на странице действия"}
                parts = ", ".join(dict.fromkeys(
                    f"{row['target']} ({row['where']['path']}:{row['where']['start_line']}-{row['where']['end_line']}"
                    f"; {why[row['kind']]})" for row in rows))
                gap("unresolved_control_link", [key], list(dict.fromkeys(row["unit_id"] for row in rows)),
                    f"Операция «{op['name']}»: продолжение через {parts} не установлено; получатель и следующий шаг "
                    "в переданном коде не показаны. Это не внешний получатель и не завершение процесса.", "host")
        for (source, target), items in unsupported_pairs(self.effective_proposals()):
            units = list(dict.fromkeys([*ops[source]["unit_ids"], *ops[target]["unit_ids"]]))
            reasons = "; ".join(dict.fromkeys(_SUPPORT_REASONS_RU[item["reason"]] for item in items))
            cited = ", ".join(dict.fromkeys(self.cited_at(ref) for item in items for ref in item["evidence_ids"]))
            sources = "; ".join(f"«{ops[key]['name']}» — " + ", ".join(self.unit_address(uid)
                                                                      for uid in ops[key]["unit_ids"])
                                for key in (source, target))
            gap("unsupported_transition", [source, target], units,
                f"Предложенный переход «{ops[source]['name']}» → «{ops[target]['name']}» "
                f"({', '.join(dict.fromkeys(item['basis'] for item in items))}) не принят в порядок действий: "
                f"{reasons}. Предложения {', '.join(item['id'] for item in items)} сохранены неактивными; цитаты: "
                f"{cited}; код действий: {sources}. Порядок и условие между этими действиями не установлены.", "host")
        fragments, seen = [], set()
        for key in ops:
            if key in seen:
                continue
            members, stack = [], [key]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                members.append(current)
                stack.extend(sorted(neighbours[current] - seen))
            ordered = [item for item in ops if item in set(members)]
            fragments.append({"id": stable_id("afr", ordered), "operation_ids": ordered,
                              "entry_ids": [item for item in ordered if not incoming[item]]})
        return gaps, {"entries": [key for key in ops if not incoming[key]],
                      "terminals": [key for key, op in ops.items() if op["exit"] == "terminal"],
                      "fragments": fragments}

    def derive_facts(self, stage_of):
        """(facts, fact rows, mapping links, guidance links): facts at accepted operation boundaries, plus one fact
        per supplied obligation that no accepted operation realizes; never from generated text alone."""
        obligations = (self.obligations or {}).get("items", [])
        statuses = self.composed["statuses"] if self.composed is not None else {}
        proposals = []
        if self.composed is not None:
            for index, item in enumerate(self.composed["payload"]["facts"], 1):
                proposals.append({"id": stable_id("afc", self.composed["key"], index), "origin": "compose",
                                  "operation_ids": [self.ops[number - 1]["id"] for number in item["operations"]],
                                  "obligation": None if item["obligation"] is None else obligations[item["obligation"] - 1],
                                  "guidance": self.composed["guidance"][index - 1],
                                  **{key: item[key] for key in ("meaning", "rationale", "scope", "condition",
                                                                "metric_ids", "evidence_alternatives")},
                                  "lifecycle_roles": _unique(item["lifecycle_roles"])})
        else:
            for op in self.ops:
                proposals.append({"id": stable_id("afh", op["id"]), "origin": "host_boundary",
                                  "operation_ids": [op["id"]], "obligation": None, "guidance": [],
                                  "meaning": f"Операция «{op['name']}» выполнена",
                                  "rationale": "Граница принятой операции из кода; факт предложен хостом без "
                                               "смысловой проверки, потому что сборка этапов не завершена.",
                                  "scope": "Каждое выполнение операции", "condition": None, "metric_ids": [],
                                  "evidence_alternatives": [], "lifecycle_roles": ["result"]})
        covered = {item["obligation"]["id"] for item in proposals if item["obligation"] is not None}
        for number, obligation in enumerate(obligations, 1):
            status = statuses.get(number, {}).get("status", "not_checked")
            if obligation["id"] not in covered and status != "matched":
                proposals.append({"id": stable_id("afo", obligation["id"]), "origin": "obligation",
                                  "operation_ids": [], "obligation": obligation, "guidance": [],
                                  "meaning": obligation["text"],
                                  "rationale": ("Утверждённое требование; принятые операции его не реализуют"
                                                if status == "not_found" else
                                                "Утверждённое требование; принятые операции реализуют его частично"
                                                if status == "partial" else
                                                "Утверждённое требование; сопоставление с операциями не выполнено"),
                                  "scope": "Анализируемый исходный код", "condition": None, "metric_ids": [],
                                  "evidence_alternatives": [], "lifecycle_roles": []})
        facts, rows, links, guidance = [], [], {}, {}
        for item in proposals:
            stage_id = stage_of[item["operation_ids"][0]] if item["operation_ids"] else None
            indexes = [index for index, mapping in enumerate(self.mappings)
                       if mapping["operation_id"] in item["operation_ids"]
                       and (not item["lifecycle_roles"] or mapping["lifecycle_role"] in item["lifecycle_roles"])]
            obligation = item["obligation"]
            requirement = (Assessment(state=FindingState.ACCEPTED, decision_id=obligation["id"],
                                      reason="Human-approved obligation supplied explicitly; its match to code is "
                                             "a model proposal checked only for identity")
                           if obligation is not None else
                           Assessment(state=FindingState.PROPOSED,
                                      reason="Proposed at the boundary of accepted code operations; not an approved "
                                             "requirement"))
            fact = _build(f"facts[{len(facts)}]", RequiredFact, id=item["id"], meaning=item["meaning"],
                          rationale=item["rationale"], scope=item["scope"], condition=item["condition"],
                          applicability=Applicability.UNKNOWN, requirement=requirement,
                          metric_ids=item["metric_ids"], evidence_alternatives=item["evidence_alternatives"],
                          instrumentation="unknown", opportunities=Opportunities(), delivery="unknown",
                          quality="unknown", evidence_ids=[], stage_id=stage_id,
                          event_keys=_unique(self.mappings[index]["event_key"] for index in indexes),
                          lifecycle_roles=item["lifecycle_roles"])
            facts.append(fact)
            links[fact.id] = indexes
            guidance[fact.id] = list(item["guidance"])
            rows.append({"id": fact.id, "origin": item["origin"], "stage_id": stage_id,
                         "operation_ids": list(item["operation_ids"]),
                         "obligation_id": None if obligation is None else obligation["id"],
                         "lifecycle_roles": list(item["lifecycle_roles"]), "mapping_indexes": indexes})
        return facts, rows, links, guidance

    def obligation_rows(self, fact_rows):
        if self.obligations is None:
            return None
        statuses = self.composed["statuses"] if self.composed is not None else {}
        rows = []
        for number, item in enumerate(self.obligations["items"], 1):
            status = statuses.get(number)
            rows.append({**item, "status": "not_checked" if status is None else status["status"],
                         "operation_ids": [] if status is None else [self.ops[value - 1]["id"]
                                                                     for value in status["operations"]],
                         "question": None if status is None else status["question"],
                         "fact_ids": [row["id"] for row in fact_rows if row["obligation_id"] == item["id"]]})
        return {**{key: self.obligations[key] for key in ("schema", "source", "source_sha256", "obligations_sha256")},
                "items": rows}

    def finish(self, status=None):
        run, record = self.run, self.record
        for state in self.states.values():
            if state["state"] is None:
                state.update(_state("deferred", "not_planned"))
        stage_items, stage_of = self.derive_stages()
        for op in self.ops:
            op["stage_id"] = stage_of[op["id"]]
        for item in self.mappings:
            item["stage_id"] = stage_of[item["operation_id"]]
        for state in self.states.values():
            if state["state"] == "accounted":
                state["stage_ids"] = sorted({stage_of[key] for key in state["operation_ids"]})
        crossings = self.stage_transitions(stage_of)
        derived_facts, fact_rows, links, guidance = self.derive_facts(stage_of)
        for index, item in enumerate(self.mappings):
            item["fact_ids"] = [fact_id for fact_id, indexes in links.items() if index in indexes]
        gaps, frontier = self.frontier()
        composed = self.composed["payload"] if self.composed is not None else None
        if composed is not None:
            for index, item in enumerate(composed["scenarios"], 1):
                members = [self.ops[number - 1]["id"] for number in item["operations"]]
                self.scenarios.append({
                    "id": stable_id("asc", self.composed["key"], index), "name": item["name"],
                    "description": item["description"], "operation_ids": members,
                    "transition_ids": [edge["id"] for edge in self.edges
                                       if edge["from"] in members and edge["to"] in members],
                    "obligation_id": None if item["obligation"] is None
                    else self.obligations["items"][item["obligation"] - 1]["id"],
                    "request_id": self.composed["request_id"]})
            self.groups = [{"id": stable_id("agr", self.composed["key"], index), "name": item["name"],
                            "stage_id": stage_items[item["stage"] - 1]["id"],
                            "operation_ids": [self.ops[number - 1]["id"] for number in item["operations"]],
                            "request_id": self.composed["request_id"]}
                           for index, item in enumerate(composed["groups"], 1)]
        record["code_led"].update(
            working_groups=[dict(group) for group in self.working_groups],
            stages={"source": ("compose" if composed is not None else "provisional_groups" if stage_items else "none"),
                    "items": stage_items,
                    "stage_transitions": [{key: row[key] for key in ("from_stage", "to_stage", "transition_ids")}
                                          for row in crossings]},
            facts=fact_rows, obligations=self.obligation_rows(fact_rows),
            hypotheses=[] if composed is None else [
                {"id": stable_id("ahy", self.composed["key"], index), "statement": item["statement"],
                 "status": item["status"], "operation_ids": [self.ops[number - 1]["id"] for number in item["operations"]],
                 "note": item["note"], "source": "business_hypothesis"}
                for index, item in enumerate(composed["hypotheses"], 1)],
            gaps=gaps, frontier=frontier,
            transition_support=_support_record(self.transition_proposals, self.open_links, self.transition_reviews))
        run.guidance_links["facts"] = guidance
        keys = {}
        for item in self.mappings:
            keys.setdefault(item["operation_id"], []).append(item["event_key"])
        by_op = keys
        operations = [ProcessOperation(id=op["id"], name=op["name"], stage_id=op["stage_id"], kind=op["kind"],
                                       description=op["description"], actor=op["actor"],
                                       event_keys=_unique(by_op.get(op["id"], [])),
                                       evidence_ids=_unique([*op["model_evidence_ids"], *op["unit_evidence_ids"]]),
                                       details=(None if op.get("details") is None
                                                else OperationDetails.from_dict(op["details"])))
                      for op in self.ops]
        mappings = [EventMapping(event_key=item["event_key"], business_action=item["operation_name"],
                                 operation=item["operation_name"], operation_id=item["operation_id"],
                                 stage_id=item["stage_id"], lifecycle_role=item["lifecycle_role"],
                                 occurrence_rule=item["occurrence_rule"], role_basis=item["role_basis"],
                                 role_support=item["role_support"], evidence_ids=item["evidence_ids"])
                    for item in self.mappings]
        edges = [OperationTransition(id=item["id"], from_operation=item["from"], to_operation=item["to"],
                                     condition=item["condition"], evidence_ids=item["evidence_ids"])
                 for item in self.edges]
        scenarios = [ProcessScenario(id=item["id"], name=item["name"], description=item["description"],
                                     operation_ids=item["operation_ids"], transition_ids=item["transition_ids"])
                     for item in self.scenarios]
        stages = []
        for item in stage_items:
            cited = _unique(ref for op in operations if op.stage_id == item["id"] for ref in op.evidence_ids)
            stages.append(Stage(
                id=item["id"], name=item["name"],
                required=Assessment(reason="Derived from accepted code operations; not a stated business requirement"),
                implemented=Assessment(state=FindingState.PROPOSED, evidence_ids=cited,
                                       reason="Atomic operations of this stage cite source text; semantics and runtime "
                                              "reachability are unverified") if cited
                else Assessment(reason="No cited source text for this derived stage"),
                observed=Assessment(reason=_UNOBSERVED)))
        stage_edges = [Transition(from_stage=row["from_stage"], to_stage=row["to_stage"],
                                  condition=row["conditions"][0] if len(row["conditions"]) == 1 else None,
                                  evidence_ids=row["evidence_ids"]) for row in crossings]
        facts = derived_facts
        process = composed["process"] if composed is not None else {"purpose": None, "boundaries": None,
                                                                    "case_definition": None}
        record["operations"] = [dict(op) for op in self.ops]
        record["event_mappings"] = [dict(item) for item in self.mappings]
        record["transitions"] = copy.deepcopy(self.edges)  # the active proposals, as in transition_support
        record["groups"] = [dict(item) for item in self.groups]
        record["status"] = status or self.status()
        unknowns = _unique([*self.map.unknowns, *self.unknowns, *self.status_lines()])
        cited = _unique([*(ref for op in operations for ref in op.evidence_ids),
                         *(ref for op in operations for ref in _detail_evidence(op.details)),
                         *(ref for item in mappings for ref in item.evidence_ids),
                         *(ref for item in edges for ref in item.evidence_ids)])
        units_without_logs = sum(not self.unit_sites(self.units[identifier]) for identifier in self.order)
        try:
            refined = replace(self.map, purpose=process["purpose"], boundaries=process["boundaries"],
                              case_definition=process["case_definition"], stages=stages, transitions=stage_edges,
                              operations=operations, event_mappings=mappings, operation_transitions=edges,
                              scenarios=scenarios, unknowns=unknowns, evidence_ids=cited)
            validate_evidence_links(refined, list({**run.evidence, **self.staged}.values()))
        except (ContractError, ValueError) as exc:
            # Nothing is merged: decisions stay inspectable as proposals, never as operations of the map.
            record["status_reasons"].append(f"merge_rejected:{getattr(exc, 'code', type(exc).__name__)}")
            record.update(status="failed", operations=[], event_mappings=[], transitions=[], groups=[])
            for state in self.states.values():
                if state["state"] == "accounted":
                    state.update(state="request_failed", reason="merge_rejected",
                                 proposed_operation_ids=state["operation_ids"], operation_ids=[], stage_ids=[])
            # With no operation merged, only the approved obligations keep their own (unchecked) facts.
            self.ops, self.mappings, self.edges, self.composed = [], [], [], None
            self.transition_proposals, self.open_links, self.transition_reviews = [], [], []  # no page state is merged
            kept, kept_rows, kept_links, kept_guidance = self.derive_facts({})
            record["code_led"].update(stages={"source": "none", "items": [], "stage_transitions": []},
                                      facts=kept_rows, obligations=self.obligation_rows(kept_rows), hypotheses=[],
                                      gaps=[], frontier={"entries": [], "terminals": [], "fragments": []},
                                      transition_support=_support_record([], []))
            run.guidance_links["facts"] = kept_guidance
            run.fact_links = kept_links
            _summarise(record, catalog=self.catalog, appearances=self.appearances,
                       units_without_logs=units_without_logs)
            refined = replace(self.map, unknowns=_unique([*self.map.unknowns, "Результат атомарных страниц не "
                                                          "согласован с картой и не применён; решения сохранены в "
                                                          "atomic_map.json."]))
            if run.trace:
                run.trace("direct_atomic.finished", {"status": record["status"], "summary": record["summary"]})
            return refined, kept, record
        run.evidence.update(self.staged)
        run.fact_links = links
        _summarise(record, catalog=self.catalog, appearances=self.appearances, units_without_logs=units_without_logs)
        if run.trace:
            run.trace("direct_atomic.finished", {"status": record["status"], "summary": record["summary"]})
        return refined, facts, record

    def status(self):
        states = list(self.states.values())
        open_units = [state for state in states if state["state"] in ("deferred", "request_failed")]
        failed_requests = [item for item in self.record["requests"] if item["kind"] == "atomic"
                           and item["outcome"] not in _OUTCOMES_ACCEPTED]
        accepted = [item for item in self.record["requests"] if item["kind"] == "atomic"
                    and item["outcome"] in _OUTCOMES_ACCEPTED and "source_observation" not in item]
        needed = [state for state in states if state["state"] != "triaged"]
        blocking = self.open_scope_blocking()
        if open_units:
            self.record["status_reasons"].append("units_without_decision")
        if failed_requests:
            self.record["status_reasons"].append("atomic_requests_failed")
        if blocking:
            self.record["status_reasons"].append("declaration_inventory_unknown")
        led = self.record["code_led"]
        # Without the composition the stages are provisional groups and the facts host boundaries: truthful partial.
        provisional = bool(self.ops) and led["stages"]["source"] != "compose"
        if provisional:
            self.record["status_reasons"].append("final_grouping_incomplete")
        if led["gaps"]:
            self.record["status_reasons"].append("open_gaps")
        if not open_units and not failed_requests and (accepted or not needed):
            return "partial" if blocking or provisional or led["gaps"] else "complete"
        return "partial" if accepted else "failed"

    def open_scope_blocking(self):
        """Source files whose unknown declaration inventory keeps the map from being complete."""
        return ((self.record.get("declaration_coverage") or {}).get("open_scope") or {}).get("blocking", 0)
