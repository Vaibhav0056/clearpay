"""Generates synthetic PDF invoices with exact expected outcomes (see README "Edge cases")."""
import io
import random
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

OUT=Path(__file__).parent/"fixtures/invoices"
OUT.mkdir(parents=True,exist_ok=True)

def make(name,fields,items):
    p=OUT/name
    c=canvas.Canvas(str(p),pagesize=A4)
    c.setFont("Helvetica-Bold",18)
    c.drawString(46,800,"SYNTHETIC SUPPLIER INVOICE - DEMO")
    y=750
    c.setFont("Courier",10)
    for k,v in fields:
        c.drawString(46,y,f"{k}: {v}")
        y-=22
    y-=14
    for item in items:
        c.drawString(46,y,"ITEM|"+"|".join(str(x) for x in item))
        y-=22
    c.save()
    return p

common=[("Invoice Date","2026-11-10"),("Vendor Name","Acme Supplies Pvt Ltd"),("Vendor Tax ID","TAX-ACME"),("PO Number","PO-1007"),("Currency","INR")]

def write(name,inv,qty,total,po="PO-1007",vendor="Acme Supplies Pvt Ltd",tax="TAX-ACME",sku="CHAIR-B24"):
    fields=[("Invoice Number",inv)]+[(k,(po if k=="PO Number" else vendor if k=="Vendor Name" else tax if k=="Vendor Tax ID" else v)) for k,v in common]
    fields.extend([("Subtotal",str(total)),("Tax Amount","0.00"),("Invoice Total",str(total))])
    return make(name,fields,[("Ergonomic office chair mesh",sku,qty,"2500.00",total)])


def write_tax_inclusive():
    """09: a realistic Indian tax invoice from Nova that quotes desks GST-INCLUSIVE (₹7,080 = ₹6,000 + 18%),
    while the PO price is pre-tax ₹6,000. Nothing says "prices include tax: yes" - the reader (a vision
    model) has to infer it from the "Rate (incl. GST)" column and the tax summary. Native text layer, but
    not the labelled demo format, so the offline parser cannot read it and the invoice goes to review."""
    p=OUT/"09_gst_inclusive_pricing.pdf"
    c=canvas.Canvas(str(p),pagesize=A4)
    c.setFont("Helvetica-Bold",19); c.drawString(50,790,"Nova Office Systems")
    c.setFont("Helvetica",9)
    c.drawString(50,776,"14 MG Road, Bengaluru 560001  |  GSTIN / Tax ID: TAX-NOVA  |  accounts@nova.example")
    c.line(50,766,545,766)
    c.setFont("Helvetica-Bold",14); c.drawString(420,738,"TAX INVOICE")
    c.setFont("Helvetica",10)
    for i,(k,v) in enumerate([("Invoice No.","NOS/26-27/0091"),("Invoice Date","12/11/2026"),("Buyer PO","PO-2001"),("Place of supply","Karnataka")]):
        c.drawString(360,714-i*16,k); c.drawRightString(545,714-i*16,v)
    c.drawString(50,714,"Billed to: Example Buyer Ltd")
    c.drawString(50,698,"Accounts Payable, Pune")
    y=620
    c.setFillGray(0.88); c.rect(50,y-5,495,20,fill=1,stroke=0); c.setFillGray(0)
    c.setFont("Helvetica-Bold",9)
    for x,h in [(55,"#"),(72,"HSN/SKU"),(140,"Description"),(330,"Qty"),(370,"Rate (incl. GST)"),(470,"Amount (INR)")]: c.drawString(x,y,h)
    c.setFont("Helvetica",10)
    for x,v in [(55,"1"),(72,"DESK-01"),(140,"Adjustable workstation desk"),(335,"2")]: c.drawString(x,y-24,v)
    c.drawRightString(445,y-24,"7,080.00"); c.drawRightString(540,y-24,"14,160.00")
    c.line(50,y-40,545,y-40)
    c.setFont("Helvetica-Bold",10); c.drawString(360,y-60,"Invoice total"); c.drawRightString(540,y-60,"14,160.00")
    c.setFont("Helvetica-Oblique",9); c.drawString(50,y-60,"All prices are inclusive of GST @ 18%.")
    y2=y-110
    c.setFont("Helvetica-Bold",10); c.drawString(50,y2,"Tax summary (included in the prices above)")
    c.setFont("Helvetica",10)
    for i,(k,v) in enumerate([("Taxable value","12,000.00"),("CGST @ 9%","1,080.00"),("SGST @ 9%","1,080.00"),("Total GST","2,160.00")]):
        c.drawString(70,y2-20-i*16,k); c.drawRightString(260,y2-20-i*16,v)
    c.setFont("Helvetica",9); c.drawString(50,y2-110,"Amount in words: Indian Rupees Fourteen Thousand One Hundred Sixty Only")
    c.setFont("Helvetica-Oblique",8); c.drawString(50,90,"Synthetic demo document. Not a real invoice.")
    c.save()
    return p


def write_scanned():
    """08: a different vendor layout (table, letterhead, 'Your Order Ref') photographed/scanned to an
    image-only page. No text layer exists, so only OCR or a vision model can read it."""
    buf=io.BytesIO()
    c=canvas.Canvas(buf,pagesize=A4)
    c.setFont("Helvetica-Bold",20); c.drawString(50,790,"ACME SUPPLIES PVT LTD")
    c.setFont("Helvetica",9)
    c.drawString(50,775,"Plot 14, Industrial Area Phase II, Pune 411019   |   GSTIN / Tax ID: TAX-ACME")
    c.line(50,765,545,765)
    c.setFont("Helvetica-Bold",14); c.drawString(400,735,"TAX INVOICE")
    c.setFont("Helvetica",10)
    for i,(k,v) in enumerate([("Invoice #","INV-008"),("Dated","12-Nov-2026"),("Your Order Ref","PO-1007")]):
        c.drawString(380,712-i*16,k); c.drawString(470,712-i*16,v)
    c.drawString(50,712,"Bill to: Example Buyer Ltd, Accounts Payable")
    y=640
    c.setFillGray(0.85); c.rect(50,y-4,495,18,fill=1,stroke=0); c.setFillGray(0)
    c.setFont("Helvetica-Bold",10)
    for x,h in [(55,"Item code"),(140,"Description"),(345,"Qty"),(395,"Rate"),(475,"Amount")]: c.drawString(x,y,h)
    c.setFont("Helvetica",10)
    c.drawString(55,y-24,"CHAIR-B24"); c.drawString(140,y-24,"Ergonomic office chair mesh")
    c.drawString(345,y-24,"4"); c.drawString(395,y-24,"2,500.00"); c.drawString(475,y-24,"10,000.00")
    c.line(50,y-40,545,y-40)
    for i,(k,v) in enumerate([("Taxable value","10,000.00"),("IGST @ 18%","1,800.00"),("Amount payable","11,800.00")]):
        c.setFont("Helvetica-Bold" if i==2 else "Helvetica",10)
        c.drawString(360,y-64-i*18,k); c.drawRightString(540,y-64-i*18,v)
    c.setFont("Helvetica-Oblique",8); c.drawString(50,90,"Synthetic demo document. Not a real invoice.")
    c.save()

    import pymupdf
    from PIL import Image, ImageFilter
    with pymupdf.open(stream=buf.getvalue(),filetype="pdf") as vector:
        pix=vector[0].get_pixmap(dpi=110,colorspace=pymupdf.csGRAY)
    img=Image.frombytes("L",(pix.width,pix.height),pix.samples)
    img=img.rotate(0.8,expand=False,fillcolor=255,resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.6))
    rnd=random.Random(8)  # deterministic speckle so re-generation is reproducible
    px=img.load()
    for _ in range(4000):
        x,y=rnd.randrange(img.width),rnd.randrange(img.height); px[x,y]=rnd.choice((90,160,200))
    png=io.BytesIO(); img.save(png,format="PNG")
    with pymupdf.open() as doc:
        doc.new_page(width=595,height=842).insert_image(pymupdf.Rect(0,0,595,842),stream=png.getvalue())
        doc.save(OUT/"08_scanned_different_layout.pdf")


if __name__=="__main__":
    write("01_happy_path.pdf","INV-001",10,"25000.00")
    write("02_partial_po_65k.pdf","INV-002",26,"65000.00")
    write("03_po_over_capacity.pdf","INV-003",16,"40000.00")
    write("04_same_invoice_different_pdf.pdf","INV-001",10,"25000.00")
    write("05_missing_po.pdf","INV-005",1,"2500.00",po="")
    write("06_unapproved_vendor.pdf","INV-006",1,"2500.00",vendor="Unknown Vendor",tax="TAX-UNKNOWN")
    write("07_wrong_sku.pdf","INV-007",1,"2500.00",sku="NOT-CHAIR")
    write_scanned()
    write_tax_inclusive()
    print("Wrote",len(list(OUT.glob('*.pdf'))),"PDFs to",OUT)
