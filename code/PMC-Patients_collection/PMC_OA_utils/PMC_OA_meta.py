import os
import pandas as pd
import re
import json
from tqdm import trange, tqdm


# PMC_OA dataset downloaded from https://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_bulk/
data_dir = "/mnt/hdd/sdc/ssim/pmc_bulk_downloads"
# Keep derived metadata next to extractor output (not inside bulk download tree)
meta_dir = "/mnt/hdd/sdc/ssim/meta_data"
os.makedirs(meta_dir, exist_ok=True)
# PMID/PMCID converter downloaded from https://www.ncbi.nlm.nih.gov/pmc/pmctopmid/
ID_converter = pd.read_csv(os.path.join(data_dir, "PMC-ids.csv"))
ID_dict = {}
for i in trange(len(ID_converter)):
    PMC_id = ID_converter['PMCID'].iloc[i]
    PMID = str(ID_converter['PMID'].iloc[i])
    # Filter those without PMID
    if PMID != '0' and PMID != 'nan' and PMID != '':
        ID_dict[PMC_id] = PMID.replace('.0', '')

subsets = ["oa_comm", "oa_noncomm", "oa_other"]

file_paths = []
PMIDs = []
PMID_set = set()
Licenses = []
for subset in subsets:
    directory = os.path.join(data_dir, subset)
    csvs = filter(lambda x: x.endswith(".csv"), os.listdir(directory))
    
    for csv in tqdm(list(csvs)):
        filelist = pd.read_csv(os.path.join(directory, csv))
        for i in range(len(filelist)):
            file_path = filelist['Article File'].iloc[i]
            PMID = str(filelist['PMID'].iloc[i])
            License = filelist['License'].iloc[i]
            if not PMID:
                PMID = '0'

            PMC_id = file_path[(file_path.find("/PMC") + 1) : -4]
            if PMC_id in ID_dict.keys():
                if ID_dict[PMC_id] != PMID:
                    PMID = ID_dict[PMC_id]
            
            if (PMID == '0') or (PMID in PMID_set):
                continue
            
            file_paths.append(directory.split('/')[-1] + '/' + file_path)
            PMIDs.append(PMID)
            PMID_set.add(PMID)
            Licenses.append(License)

try:
    data = pd.DataFrame({"file_path": file_paths, "PMID": PMIDs, "License": Licenses})
    out_path = os.path.join(meta_dir, "PMC_OA_meta.csv")
    data.to_csv(out_path)
    print(f"Wrote {len(data)} rows to {out_path}")
except Exception as e:
    print(e)
    raise
