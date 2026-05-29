import json

from routes._entities import harvest_from_summary_json


def test_harvest_from_summary_json_reads_radar_schema():
    data = {
        "radar": [
            {
                "target": "$MYTHOS",
                "source": "deployer reply",
                "signal": "@mythosrouter beta with CA 0xb942b75a602fa318ac091370d93d9143ba345ba3",
            }
        ],
        "needs_context": [
            {"clue": "0xaf1e52927d724fd34773bd53ada57f4c2b742069"}
        ],
        "follows": {
            "low_convergence_projects": [
                {"target": "@BioLLM_", "judgment": "AI bio LLM project"}
            ]
        },
    }

    out = harvest_from_summary_json(json.dumps(data))

    assert "MYTHOS" in out["symbol"]
    assert "mythosrouter" in out["handle"]
    assert "biollm_" in out["handle"]
    assert "0xb942b75a602fa318ac091370d93d9143ba345ba3" in out["ca"]
    assert "0xaf1e52927d724fd34773bd53ada57f4c2b742069" in out["ca"]
