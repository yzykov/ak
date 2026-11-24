#!/usr/bin/env python3
"""Safely rename imported Bitrix product CODE values. Dry run by default."""
import argparse, json, os
from pathlib import Path
from catalog_sync_core.bitrix import BitrixClient
from catalog_sync_core.runtime import config_load
from catalog_sync_core.taxonomy import Taxonomy

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default='config/catalog.json');p.add_argument('--apply',action='store_true');p.add_argument('--limit',type=int,default=0);p.add_argument('--element-id',type=int);p.add_argument('--report',default='output/product_codes.jsonl');a=p.parse_args()
    cfg=config_load(Path(a.config));client=BitrixClient(cfg['target']['api_base'],os.getenv('AKOMFORT_IMPORT_TOKEN',''),timeout=int(cfg['network']['timeout_sec']))
    meta=client.metadata();tc=cfg['taxonomy'];taxonomy=Taxonomy(meta['sections'],cfg['target']['excluded_section_codes'],minimum_score=tc['minimum_match_score'],minimum_margin=tc['minimum_margin'],aliases=tc.get('section_aliases',{}))
    products=client.products(cfg['target']['catalog_url'],taxonomy);path=Path(a.report);path.parent.mkdir(parents=True,exist_ok=True);count=0
    with path.open('a',encoding='utf-8') as out:
        for item in products:
            if not item.xml_id.startswith(('valmo:','gkgross:','liftnet:')) or not item.bitrix_id or (a.element_id and item.bitrix_id!=a.element_id):continue
            if a.limit and count>=a.limit:break
            payload={'element_id':item.bitrix_id,'expected_xml_id':item.xml_id,'apply':a.apply}
            try:
                r=client.http.post(client.base+'repair_product_code.php',json=payload,timeout=client.timeout,allow_redirects=False);result=r.json();result['http_status']=r.status_code
            except Exception as exc:result={'error':str(exc)}
            result['element_id']=item.bitrix_id;out.write(json.dumps(result,ensure_ascii=False)+'\n');out.flush();count+=1
            print(item.bitrix_id,result)
    print('Processed:',count,'Report:',path)
if __name__=='__main__':main()
