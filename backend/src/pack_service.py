from sqlalchemy import select,func
from fastapi import HTTPException
from .models import Edition,Classroom,Pack,PackLine,Copy,Assignment,Purchase,PackCancellation
from .common import inventory_lock,compatible,lock,add,pack,audit

def create(s,d,u):
    room=s.get(Classroom,d.classroom_id)
    if not room:raise HTTPException(404,'班级不存在')
    subjects=[]
    for line in d.lines:
        if not s.get(Edition,line.edition_id):raise HTTPException(404,'课程版次不存在')
        subjects.append(s.get(Edition,line.edition_id).subject)
        if line.quantity>room.size:raise HTTPException(409,'课程份数不能超过班级人数')
    if len(set(subjects))!=len(subjects):raise HTTPException(409,'同一课程不能用多个版次重复列入配套')
    x=Pack(code=d.code,classroom_id=d.classroom_id,term=d.term);s.add(x);s.flush()
    for line in d.lines:s.add(PackLine(pack_id=x.id,**line.model_dump()))
    s.flush();audit(s,u,'建立班级课程配套 '+d.code);return pack(x)
def reserve(s,id,u):
    inventory_lock(s);p=lock(s,Pack,id)
    if p.state in ('allocated','cancelled'):raise HTTPException(409,'完整配套不可重复领用，已撤销配套不可续配')
    missing=[];reserved=[]
    for line in s.scalars(select(PackLine).where(PackLine.pack_id==id).order_by(PackLine.id)):
        assigned=s.scalar(select(func.count()).select_from(Assignment).where(Assignment.line_id==line.id,Assignment.replacement_for_id.is_(None),Assignment.state!='cancelled'))
        needed=max(0,line.quantity-assigned)
        copies=s.scalars(select(Copy).where(Copy.edition_id.in_(compatible(s,line.edition_id)),Copy.condition=='usable',Copy.state=='available').order_by(Copy.id).limit(needed).with_for_update()).all()if needed else[]
        for copy in copies:
            copy.state='reserved';a=Assignment(line_id=line.id,copy_id=copy.id,deposit_cents=s.get(Edition,line.edition_id).deposit_cents);s.add(a);s.flush();reserved.append(a.id)
        shortage=needed-len(copies)
        if shortage:
            po=s.scalar(select(Purchase).where(Purchase.line_id==line.id))
            if po:po.ordered=max(po.ordered,po.received+shortage);po.state='open'if po.received<po.ordered else'received'
            else:s.add(Purchase(line_id=line.id,ordered=shortage))
            missing.append({'line_id':line.id,'quantity':shortage})
    p.state='partial'if missing else'allocated';audit(s,u,'锁定可用单本并登记缺册 '+p.code);return pack(p)|{'new_assignment_ids':reserved,'shortages':missing}
def cancel(s,id,d,u):
    # 库存父锁保证撤销与领用/退书/到货串行；后到者读到先办结者的提交状态。
    inventory_lock(s)
    p=lock(s,Pack,id)
    replay=s.scalar(select(PackCancellation).where(PackCancellation.pack_id==id))
    if replay:
        # 幂等重试：撤销已办结，直接返回首次结果，不再释放库存或改补购数。
        return pack(p)|{'cancellation':pack(replay),'replayed':True}
    if p.state=='cancelled':raise HTTPException(409,'配套已撤销但缺少撤销记录，请核对审计')
    lines=list(s.scalars(select(PackLine).where(PackLine.pack_id==id).order_by(PackLine.id)))
    line_ids=[l.id for l in lines]
    # 与 reserve/receive 相同的加锁顺序：库存父锁 → Pack → Purchase → Assignment → Copy。
    purchases=list(s.scalars(select(Purchase).where(Purchase.line_id.in_(line_ids)).order_by(Purchase.id).with_for_update()))if line_ids else[]
    assignments=list(s.scalars(select(Assignment).where(Assignment.line_id.in_(line_ids)).order_by(Assignment.id).with_for_update()))if line_ids else[]
    copies={c.id:c for c in s.scalars(select(Copy).where(Copy.id.in_([a.copy_id for a in assignments])).order_by(Copy.id).with_for_update())}if assignments else{}
    detail={l.id:{'line_id':l.id,'edition_id':l.edition_id,'quantity':l.quantity,'released':[],'retained':[],'purchase':None}for l in lines}
    released=retained=0
    for a in assignments:
        copy=copies.get(a.copy_id);row=detail[a.line_id]
        if a.state=='reserved':
            if copy and copy.state=='reserved':
                copy.state='available';a.state='cancelled';a.note='配套撤销，预留书回到可用库存'
                row['released'].append({'assignment_id':a.id,'copy_id':copy.id,'barcode':copy.barcode});released+=1
            else:
                # 副本已不在预留状态（到货/领用并发先办结），保留并说明原因，绝不覆盖。
                row['retained'].append({'assignment_id':a.id,'copy_id':a.copy_id,'barcode':copy.barcode if copy else None,'state':copy.state if copy else None,'reason':'副本已不在预留状态'});retained+=1
        elif a.state in ('issued','returned','lost'):
            reason={'issued':'已领用，保留在借记录','returned':'已归还，保留归还记录','lost':'已报损，保留报损记录'}[a.state]
            row['retained'].append({'assignment_id':a.id,'copy_id':a.copy_id,'barcode':copy.barcode if copy else None,'state':a.state,'reason':reason});retained+=1
    stopped=0
    for po in purchases:
        row=detail[po.line_id];outstanding=max(0,po.ordered-po.received)
        if po.state=='open':
            po.state='cancelled';stopped+=outstanding
        row['purchase']={'purchase_id':po.id,'ordered':po.ordered,'received':po.received,'not_received':outstanding,'state':po.state,'stopped':outstanding if po.state=='cancelled'else 0}
    for row in detail.values():row['never_reserved']=max(0,row['quantity']-len(row['released'])-len(row['retained']))
    p.state='cancelled'
    log=PackCancellation(pack_id=p.id,actor=u.email,reason=d.reason,released=released,retained=retained,stopped=stopped,detail={'lines':list(detail.values())})
    s.add(log);s.flush();audit(s,u,'撤销班级配套 '+p.code+'：释放预留'+str(released)+'本，保留'+str(retained)+'本，停止收货'+str(stopped)+'本')
    return pack(p)|{'cancellation':pack(log),'replayed':False}
def receive(s,id,d,u):
    inventory_lock(s)
    po=s.get(Purchase,id)
    if not po:raise HTTPException(404,'业务记录不存在')
    p=lock(s,Pack,s.get(PackLine,po.line_id).pack_id)
    po=lock(s,Purchase,id);line=s.get(PackLine,po.line_id)
    if p.state=='cancelled' or po.state!='open' or d.quantity>po.ordered-po.received:raise HTTPException(409,'补购数量超过未收缺册或订单已关闭')
    for n in range(d.quantity):s.add(Copy(barcode=d.barcode_prefix+'-'+str(po.received+n+1),edition_id=line.edition_id,condition='usable',state='available',source='缺册补购PO'+str(po.id)))
    po.received+=d.quantity
    if po.received==po.ordered:po.state='received'
    s.flush();audit(s,u,'补购到货验收 '+str(po.id));return pack(po)
