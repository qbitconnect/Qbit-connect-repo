"""Definition validation tests (§4, §5, §21, §42) + condition engine (§14–§21)."""

import pytest

from app.automation.core.condition import ConditionEngine, validate_group
from app.automation.core.exceptions import ConfigurationError, ValidationError
from app.automation.core.schemas import WorkflowDefinition
from app.automation.core.workflow import validate_graph


def _graph(nodes):
    return {"nodes": nodes}


VALID_TRIGGER = {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "end"}
END = {"id": "end", "type": "END"}


def _valid(nodes):
    return WorkflowDefinition.model_validate(_graph([VALID_TRIGGER, *nodes, END]))


class TestGraphValidation:
    def test_minimal_valid_graph(self):
        parsed = _valid([])
        validate_graph(parsed, max_nodes=50)

    def test_extra_fields_rejected(self):
        with pytest.raises(Exception):
            WorkflowDefinition.model_validate(
                {"nodes": [dict(VALID_TRIGGER, evil="__import__('os')"), END]})

    def test_missing_next_node_reference(self):
        parsed = _valid([{"id": "a", "type": "ACTION", "action": "add_tag",
                          "config": {"tag": "x"}, "next_node_id": "ghost"}])
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)

    def test_cycle_rejected(self):
        parsed = _valid([
            {"id": "a", "type": "ACTION", "action": "add_tag",
             "config": {"tag": "x"}, "next_node_id": "b"},
            {"id": "b", "type": "WAIT", "duration": {"minutes": 1}, "next_node_id": "a"},
        ])
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)

    def test_unreachable_node_rejected(self):
        parsed = _valid([
            {"id": "orphan", "type": "ACTION", "action": "add_tag",
             "config": {"tag": "x"}, "next_node_id": "end"},
        ])
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)

    def test_path_must_reach_end(self):
        parsed = _valid([
            {"id": "cond", "type": "CONDITION",
             "condition": {"field": "lead.status", "operator": "equals", "value": "NEW"},
             "next_node_id": "a", "next_node_id_no": "a"},
            {"id": "a", "type": "ACTION", "action": "add_tag",
             "config": {"tag": "x"}, "next_node_id": "cond"},
        ])
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)

    def test_two_triggers_rejected(self):
        parsed = WorkflowDefinition.model_validate(
            {"nodes": [VALID_TRIGGER, dict(VALID_TRIGGER), END]})
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)

    def test_max_nodes_enforced(self):
        nodes = [VALID_TRIGGER] + [
            {"id": f"end{i}", "type": "END"} for i in range(10)
        ]
        parsed = WorkflowDefinition.model_validate({"nodes": nodes})
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=5)

    def test_condition_requires_both_branches(self):
        parsed = _valid([{"id": "cond", "type": "CONDITION",
                          "condition": {"field": "lead.status", "operator": "equals",
                                        "value": "NEW"},
                          "next_node_id": "end"}])
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)

    def test_action_requires_action_key(self):
        parsed = _valid([{"id": "a", "type": "ACTION", "config": {}, "next_node_id": "end"}])
        with pytest.raises(ValidationError):
            validate_graph(parsed, max_nodes=50)


class TestConditionValidation:
    def test_unknown_field_rejected(self):
        with pytest.raises(ConfigurationError):
            validate_group({"field": "lead.hacker_payload", "operator": "equals", "value": 1})

    def test_unknown_operator_rejected(self):
        with pytest.raises(ConfigurationError):
            validate_group({"field": "lead.status", "operator": "__lt__", "value": 1})

    def test_between_requires_pair(self):
        with pytest.raises(ConfigurationError):
            validate_group({"field": "lead.quality_score", "operator": "between",
                            "value": 5})

    def test_empty_group_rejected(self):
        with pytest.raises(ConfigurationError):
            validate_group({})

    def test_nested_group_valid(self):
        validate_group({"all": [
            {"field": "lead.quality_score", "operator": "greater_than", "value": 70},
            {"any": [
                {"field": "lead.status", "operator": "equals", "value": "NEW"},
                {"not": {"field": "lead.has_email", "operator": "equals", "value": True}},
            ]},
        ]})


def _engine(lead=None, message=None, conversation=None, campaign=None, recipient=None):
    engine = ConditionEngine()
    engine.bind({
        "lead": lambda: lead,
        "message": lambda: message,
        "conversation": lambda: conversation,
        "campaign": lambda: campaign,
        "recipient": lambda: recipient,
    })
    return engine


LEAD = {
    "status": "NEW", "quality_score": 85, "city": "Surat", "email": "a@b.test",
    "phone": None, "tags": ["Hot", "Priority"], "has_email": True, "has_phone": False,
}


class TestConditionEvaluation:
    def test_numeric_greater_than(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.quality_score", "operator": "greater_than", "value": 70})

    def test_numeric_comparison_against_string_value(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.quality_score", "operator": "greater_than", "value": "70"})

    def test_equals_missing_field_raises(self):
        with pytest.raises(ConfigurationError):
            _engine(lead=LEAD).evaluate(
                {"field": "lead.no_such_field", "operator": "equals", "value": 1})

    def test_missing_entity_raises(self):
        with pytest.raises(ConfigurationError):
            _engine().evaluate(
                {"field": "lead.status", "operator": "equals", "value": "NEW"})

    def test_contains_case_insensitive(self):
        msg = {"body": "Please send PRICING details", "channel": "WHATSAPP"}
        assert _engine(message=msg).evaluate(
            {"field": "message.body", "operator": "contains", "value": "pricing"})
        assert not _engine(message=msg).evaluate(
            {"field": "message.body", "operator": "contains", "value": "refund"})

    def test_is_empty_semantics(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.phone", "operator": "is_empty"})
        assert not _engine(lead=LEAD).evaluate(
            {"field": "lead.email", "operator": "is_empty"})

    def test_has_tag_special(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.has_tag", "operator": "equals", "value": "hot"})
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.does_not_have_tag", "operator": "equals", "value": "Contacted"})

    def test_boolean_normalisation(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.has_email", "operator": "equals", "value": "true"})
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.has_email", "operator": "equals", "value": True})

    def test_and_group(self, ):
        assert _engine(lead=LEAD).evaluate({"all": [
            {"field": "lead.quality_score", "operator": "greater_than", "value": 70},
            {"field": "lead.has_email", "operator": "equals", "value": True},
            {"field": "lead.status", "operator": "equals", "value": "NEW"},
        ]})

    def test_or_group(self):
        assert _engine(lead=LEAD).evaluate({"any": [
            {"field": "lead.status", "operator": "equals", "value": "LOST"},
            {"field": "lead.quality_score", "operator": "greater_than", "value": 70},
        ]})

    def test_not_group(self):
        assert _engine(lead=LEAD).evaluate(
            {"not": {"field": "lead.has_email", "operator": "equals", "value": False}})

    def test_between_operator(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.quality_score", "operator": "between", "value": [80, 90]})

    def test_in_operator(self):
        assert _engine(lead=LEAD).evaluate(
            {"field": "lead.city", "operator": "in", "value": ["Surat", "Baroda"]})

    def test_none_condition_is_true(self):
        assert _engine(lead=LEAD).evaluate(None)
