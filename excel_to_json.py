# -*- coding: utf-8 -*-
import os
import json
import pandas as pd
from pathlib import Path
from config import PROFILES_JSON, BASE_DIR

EXCEL_FILE = BASE_DIR / "مشاوران مرکز ویرایش 22 مرداد 1405 (3) (2).xlsx"
JSON_OUTPUT = str(PROFILES_JSON)

def convert_excel_to_json(excel_path=EXCEL_FILE, json_output=JSON_OUTPUT):
    if not os.path.exists(excel_path):
        print(f"فایل اکسل در مسیر {excel_path} یافت نشد.")
        return 0

    df = pd.read_excel(excel_path, header=None)
    output_data = []

    for row_idx in range(2, len(df)):
        row = df.iloc[row_idx]
        name = row[2]
        if pd.isna(name) or not str(name).strip() or str(name).strip() == 'nan':
            continue

        # ضریب توانمندی مشاورین (ستون 1)
        ability = row[1]
        ability_val = float(ability) if pd.notna(ability) and str(ability).strip().isdigit() or isinstance(ability, (int, float)) and not pd.isna(ability) else 2.0
        if ability_val not in [1.0, 2.0, 3.0]:
            ability_val = 2.0

        # مکان (ستون 3)
        location = str(row[3]).strip() if pd.notna(row[3]) and str(row[3]).strip() != 'nan' else 'هر دو'
        
        # تحصیلات و سوابق (ستون 4)
        edu = str(row[4]).strip() if pd.notna(row[4]) and str(row[4]).strip() != 'nan' else ''

        # حوزه‌های عمومی (ستون‌های 5 تا 17)
        gen_dict = {}
        for c in range(5, 18):
            h = str(df.iloc[1, c]).strip() if pd.notna(df.iloc[1, c]) else str(df.iloc[0, c]).strip()
            val = row[c]
            if pd.notna(val) and str(val).strip() and str(val).strip() != 'nan':
                gen_dict[h] = str(val).strip()

        # موضوعات جزئی (ستون‌های 19 تا 30)
        det_dict = {}
        for c in range(19, 31):
            h = str(df.iloc[1, c]).strip() if pd.notna(df.iloc[1, c]) else str(df.iloc[0, c]).strip()
            val = row[c]
            if pd.notna(val) and str(val).strip() and str(val).strip() != 'nan':
                det_dict[h] = str(val).strip()

        free_18 = str(row[18]).strip() if pd.notna(row[18]) and str(row[18]).strip() != 'nan' else ''
        free_31 = str(row[31]).strip() if pd.notna(row[31]) and str(row[31]).strip() != 'nan' else ''
        age = str(row[32]).strip() if pd.notna(row[32]) and str(row[32]).strip() != 'nan' else ''
        license_info = str(row[33]).strip() if pd.notna(row[33]) and str(row[33]).strip() != 'nan' else ''
        notes = str(row[34]).strip() if pd.notna(row[34]) and str(row[34]).strip() != 'nan' else ''

        all_specs = {**gen_dict, **det_dict}

        consultant = {
            "name": str(name).strip(),
            "ability": ability_val,
            "location": location,
            "education_experience": edu,
            "general_area": free_18,
            "general_area_2": "",
            "specializations": all_specs,
            "detailed_topics": free_31,
            "age_range": age,
            "license": license_info,
            "notes": notes
        }
        output_data.append(consultant)

    with open(json_output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"تعداد {len(output_data)} مشاور با موفقیت در {json_output} ذخیره شد.")
    return len(output_data)

if __name__ == "__main__":
    convert_excel_to_json()