import pytest

import parse_log


def record(body):
    return {"timestamp": "2026-10-28T11:05:09Z", "accountId": "111", "region": "us-west-2", "requestId": "r1",
            "operation": "InvokeModelWithResponseStream", "modelId": "arn:aws:bedrock:us-west-2::foundation-model/m",
            "identity": {"arn": "arn:aws:sts::111:assumed-role/Role/session"},
            "input": {"inputTokenCount": 10, "cacheWriteInputTokenCount": 30},
            "output": {"outputTokenCount": 50, "outputBodyJson": body}}


def test_streaming_usage_merges_start_split_with_final_totals():
    body = [
        {"type": "message_start", "message": {"usage": {"output_tokens": 8, "cache_creation": {
            "ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20}}}},
        {"type": "message_delta", "usage": {"output_tokens": 50}},
    ]
    ev = parse_log.build_event(record(body), "111", "us-west-2", "s3://b/k")
    assert ev["model_id"] == "m" and ev["caller"] == "Role/session"
    assert (ev["cache_write_5m_tokens"], ev["cache_write_1h_tokens"]) == (10, 20)


def test_record_without_request_id_is_skipped():
    rec = record({})
    rec["requestId"] = ""
    assert parse_log.build_event(rec, "111", "us-west-2", "s3://b/k") is None


class FakeFirehose:
    def __init__(self, failures_per_call):
        self.failures_per_call, self.calls = list(failures_per_call), []

    def put_record_batch(self, DeliveryStreamName, Records):
        self.calls.append(len(Records))
        fail = self.failures_per_call.pop(0) if self.failures_per_call else 0
        responses = [{"ErrorCode": "ServiceUnavailableException"} if i < fail else {"RecordId": "x"}
                     for i in range(len(Records))]
        return {"FailedPutCount": fail, "RequestResponses": responses}


def test_put_batch_resends_only_rejected_records(monkeypatch):
    fake = FakeFirehose([2, 0])
    monkeypatch.setattr(parse_log, "firehose", fake)
    parse_log.put_batch([{"Data": b"a"}, {"Data": b"b"}, {"Data": b"c"}], sleep=lambda s: None)
    assert fake.calls == [3, 2]


def test_put_batch_raises_when_records_keep_failing(monkeypatch):
    monkeypatch.setattr(parse_log, "firehose", FakeFirehose([1] * 10))
    with pytest.raises(RuntimeError):
        parse_log.put_batch([{"Data": b"a"}], attempts=3, sleep=lambda s: None)


@pytest.mark.parametrize("arn, key", [
    ("anthropic.claude-sonnet-4-20250514-v1:0", "anthropic.claude-sonnet-4-20250514-v1:0"),
    ("arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-haiku-20240307-v1:0", "anthropic.claude-3-haiku-20240307-v1:0"),
    ("arn:aws:bedrock:us-west-2:111122223333:inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0",
     "us.anthropic.claude-sonnet-4-20250514-v1:0"),
    ("arn:aws:bedrock:us-west-2:111122223333:application-inference-profile/8b75bgbqm6ri", "application-inference-profile/8b75bgbqm6ri"),
    ("arn:aws:bedrock:us-west-2:111122223333:imported-model/twdi0pznwga0", "imported-model/twdi0pznwga0"),
    ("arn:aws:bedrock:us-west-2:111122223333:provisioned-model/abc123", "provisioned-model/abc123"),
    ("arn:aws:bedrock:us-west-2:111122223333:custom-model/amazon.titan-text-lite-v1:0:4k/x1y2", "custom-model/amazon.titan-text-lite-v1:0:4k/x1y2"),
])
def test_model_key_keeps_resource_type_for_non_model_arns(arn, key):
    assert parse_log.model_key(arn) == key
