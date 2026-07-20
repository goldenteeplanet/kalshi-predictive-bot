from pathlib import Path

from kalshi_predictor.phase_prov14b_r1 import write_prov14b_r1_repair_preview

if __name__ == "__main__":
    print(write_prov14b_r1_repair_preview(Path("reports/phase_prov14b_r1")))
