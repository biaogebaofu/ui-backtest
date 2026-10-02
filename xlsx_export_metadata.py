"""Preserve filter/frozen-header metadata omitted by some XLSX serializers.

Only package-level view/table metadata is repaired. Cell data, formulas, caches,
styles, identifiers, relationships and the backtest calculations are untouched.
"""
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import os

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('x',NS)
def q(tag):return '{'+NS+'}'+tag

def ensure_export_metadata(path):
    path=Path(path);temp=path.with_name(path.name+'.metadata.tmp')
    changed=[]
    try:
        with ZipFile(path) as source, ZipFile(temp,'w') as target:
            for info in source.infolist():
                data=source.read(info.filename);root=None
                if info.filename.startswith('xl/tables/') and info.filename.endswith('.xml'):
                    root=ET.fromstring(data)
                    if root.find(q('autoFilter')) is None and root.get('ref'):
                        auto=ET.Element(q('autoFilter'),{'ref':root.get('ref')})
                        root.insert(0,auto);changed.append(info.filename)
                    else:root=None
                elif info.filename.startswith('xl/worksheets/sheet') and info.filename.endswith('.xml'):
                    root=ET.fromstring(data)
                    if root.find(q('sheetViews')) is None:
                        views=ET.Element(q('sheetViews'));v=ET.SubElement(views,q('sheetView'),{'workbookViewId':'0'})
                        ET.SubElement(v,q('pane'),{'ySplit':'1','topLeftCell':'A2','activePane':'bottomLeft','state':'frozen'})
                        ET.SubElement(v,q('selection'),{'pane':'bottomLeft','activeCell':'A2','sqref':'A2'})
                        index=0
                        while index<len(root) and root[index].tag in (q('sheetPr'),q('dimension')):index+=1
                        root.insert(index,views);changed.append(info.filename)
                    else:root=None
                elif info.filename=='xl/workbook.xml':
                    root=ET.fromstring(data)
                    if root.find(q('bookViews')) is None:
                        views=ET.Element(q('bookViews'));ET.SubElement(views,q('workbookView'),{'activeTab':'0'})
                        children=list(root);index=next((i for i,c in enumerate(children) if c.tag==q('sheets')),len(children))
                        root.insert(index,views);changed.append(info.filename)
                    else:root=None
                if root is not None:data=ET.tostring(root,encoding='utf-8',xml_declaration=True)
                target.writestr(info,data)
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()
    return changed
