# frimware.py
# my first e-reader. it reads /sd/book.txt and the two buttons turn pages.
# i copied the screen init from the waveshare example.

import time, os, machine, framebuf

SCK=12; MOSI=11; CS=10; DC=9; RST=4; BUSY=3
NEXT=6; PREV=7
W=800; H=480

spi=machine.SPI(1, baudrate=4000000, polarity=0, phase=0,
    sck=machine.Pin(SCK), mosi=machine.Pin(MOSI))
cs=machine.Pin(CS, machine.Pin.OUT)
dc=machine.Pin(DC, machine.Pin.OUT)
rst=machine.Pin(RST, machine.Pin.OUT)
busy=machine.Pin(BUSY, machine.Pin.IN, machine.Pin.PULL_UP)
bnext=machine.Pin(NEXT, machine.Pin.IN, machine.Pin.PULL_UP)
bprev=machine.Pin(PREV, machine.Pin.IN, machine.Pin.PULL_UP)

buf=bytearray(W*H//8)
inv=bytearray(W*H//8)
fb=framebuf.FrameBuffer(buf,W,H,framebuf.MONO_HMSB)

def cmd(c):
    dc.value(0); cs.value(0); spi.write(bytes([c])); cs.value(1)

def data(d):
    dc.value(1); cs.value(0)
    spi.write(bytes([d]) if type(d)==int else d)
    cs.value(1)

def wait():
    while busy.value()==0:
        time.sleep(0.02)

def init():
    rst.value(0); time.sleep(0.02); rst.value(1); time.sleep(0.02)
    cmd(0x01); data(bytearray([0x07,0x07,0x3F,0x3F]))
    cmd(0x06); data(bytearray([0x17,0x17,0x28,0x17]))
    cmd(0x04); time.sleep(0.1); wait()
    cmd(0x00); data(0x1F)
    cmd(0x61); data(bytearray([0x03,0x20,0x01,0xE0]))
    cmd(0x15); data(0x00)
    cmd(0x50); data(bytearray([0x10,0x07]))
    cmd(0x60); data(0x22)

def show():
    for i in range(len(buf)):
        inv[i]=buf[i]^0xFF
    cmd(0x10); data(buf)
    cmd(0x13); data(inv)
    cmd(0x12); time.sleep(0.1); wait()

def wrap(t):
    t=t.replace("\r","").replace("\n"," ")
    return [t[i:i+95] for i in range(0,len(t),95)]

def draw(n):
    fb.fill(1)
    title="my book"
    fb.text(title,10,10,0)
    p=str(n+1)+"/"+str(len(pages))
    fb.text(p,W-10-len(p)*8,10,0)
    fb.hline(10,28,W-20,0)
    y=40
    for line in pages[n]:
        fb.text(line,10,y,0)
        y+=14
    fb.hline(10,H-30,W-20,0)
    fb.text("NEXT / PREV turns page",10,H-20,0)
    show()

def main():
    global pages
    init()
    fb.fill(1); fb.text("loading...",10,10,0); show()
    try:
        try: os.umount("/sd")
        except OSError: pass
        sd=machine.SDCard(slot=0,width=1)
        os.mount(sd,"/sd")
        f=open("/sd/book.txt")
        book=f.read()
        f.close()
    except Exception as e:
        fb.fill(1); fb.text("couldnt open book.txt",10,10,0)
        fb.text(str(e),10,30,0); show()
        return
    lines=wrap(book)
    pages=[lines[i:i+28] for i in range(0,len(lines),28)]
    if not pages: pages=[[""]]
    n=0
    draw(n)
    while True:
        if bnext.value()==0:
            if n<len(pages)-1:
                n+=1; draw(n)
            while bnext.value()==0: time.sleep(0.05)
        if bprev.value()==0:
            if n>0:
                n-=1; draw(n)
            while bprev.value()==0: time.sleep(0.05)
        time.sleep(0.05)

# btw if the screen stays white check Q1, my board has the G/S/D pins swapped
main()
