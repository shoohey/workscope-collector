# Release Check Report

**Overall**: PASS — release allowed

| Check | Status | Critical | Detail |
|---|---|---|---|
| pytest_all | ✅ | yes | 349 passed in 1.99s |
| pii_safety_tests | ✅ | yes | ============================== 6 passed in 0.25s =============================== |
| profiles_valid | ✅ | yes | 12 profiles validated |
| required_files | ✅ | yes | all 16 files present |
| app_rules | ✅ | yes | 8 categories: ['saas_web', 'saas_desktop', 'erp', 'industry_medical', 'industry_accounting', 'office', 'browser', 'dev'] |
| config_constants | ✅ | no | constants present |
| requirements | ✅ | no | 12 dependencies declared |

### profiles_valid details
- accounting: rules=17, whitelist_keys=['common_terms']
- base: rules=11, whitelist_keys=[]
- care_welfare: rules=15, whitelist_keys=['care_terms']
- construction: rules=15, whitelist_keys=['construction_terms']
- generic: rules=13, whitelist_keys=[]
- hr: rules=16, whitelist_keys=['common_terms']
- legal: rules=17, whitelist_keys=['legal_terms']
- manufacturing: rules=16, whitelist_keys=['manufacturing_terms']
- medical_dental: rules=15, whitelist_keys=['dental_terms', 'common_drug_names']
- pharmacy: rules=14, whitelist_keys=['drug_names']
- real_estate: rules=16, whitelist_keys=['real_estate_terms']
- sales: rules=16, whitelist_keys=['industry_terms']

### config_constants details
- DEFAULT_PROFILE=''
- CUSTOMER_NAME=''
- UPLOAD_ENDPOINT=''

### requirements details
- mss==9.0.1
- pywin32>=307,<312
- psutil==5.9.8
- Pillow==10.2.0
- numpy==1.26.4
- opencv-python-headless==4.9.0.80
- paddlepaddle>=2.6.2,<2.7
- paddleocr==2.7.3
- pystray==0.19.5
- pydantic==2.6.1
- python-dateutil==2.8.2
- pyinstaller==6.4.0
