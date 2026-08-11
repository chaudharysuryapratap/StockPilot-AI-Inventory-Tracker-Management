from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app


class BedrockNarrator:
    """Adds a concise explanation to a deterministic inventory forecast.

    The numeric forecast is intentionally calculated locally. A language model
    is useful for clear operational wording, not as the source of stock numbers.
    """

    @staticmethod
    def explain(metrics: dict[str, Any]) -> str | None:
        if not current_app.config["BEDROCK_ENABLED"]:
            return None

        system_prompt = (
            "You are an inventory operations assistant. Use only the supplied "
            "metrics. Write one concise actionable sentence for a small-business "
            "owner. Do not change, invent, or round any number. Do not mention AI."
        )
        try:
            client = boto3.client(
                "bedrock-runtime", region_name=current_app.config["AWS_REGION"]
            )
            response = client.converse(
                modelId=current_app.config["BEDROCK_MODEL_ID"],
                system=[{"text": system_prompt}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": json.dumps(metrics, default=str)}],
                    }
                ],
                inferenceConfig={"maxTokens": 140, "temperature": 0.1},
            )
            content = response["output"]["message"]["content"]
            text = next((part.get("text") for part in content if part.get("text")), None)
            return text.strip() if text else None
        except (ClientError, BotoCoreError, KeyError, StopIteration) as error:
            current_app.logger.warning("Bedrock narrative skipped: %s", error)
            return None

    @staticmethod
    def answer(question: str, context: dict[str, Any]) -> str | None:
        """Answer a dashboard question using only the server-built context snapshot."""
        if not current_app.config["BEDROCK_ENABLED"]:
            return None
        system_prompt = (
            "You are StockPilot's inventory analyst. Answer only from the supplied "
            "dashboard context. Be concise and operational. If the context does not "
            "support the answer, say so. Never invent stock, demand, dates, or accuracy values."
        )
        try:
            client = boto3.client(
                "bedrock-runtime", region_name=current_app.config["AWS_REGION"]
            )
            response = client.converse(
                modelId=current_app.config["BEDROCK_MODEL_ID"],
                system=[{"text": system_prompt}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": json.dumps(
                                    {"question": question, "dashboard_context": context},
                                    default=str,
                                )
                            }
                        ],
                    }
                ],
                inferenceConfig={"maxTokens": 300, "temperature": 0.1},
            )
            content = response["output"]["message"]["content"]
            text = next((part.get("text") for part in content if part.get("text")), None)
            return text.strip() if text else None
        except (ClientError, BotoCoreError, KeyError, StopIteration) as error:
            current_app.logger.warning("Bedrock dashboard answer skipped: %s", error)
            return None
