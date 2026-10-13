#!/usr/bin/env python3
"""Minimal PIC18 disassembler (PIC18F4680 SFR names). Usage: dis.py file.hex > out.asm"""
import sys

SFR = {
0xFFF:'TOSU',0xFFE:'TOSH',0xFFD:'TOSL',0xFFC:'STKPTR',0xFFB:'PCLATU',0xFFA:'PCLATH',0xFF9:'PCL',
0xFF8:'TBLPTRU',0xFF7:'TBLPTRH',0xFF6:'TBLPTRL',0xFF5:'TABLAT',0xFF4:'PRODH',0xFF3:'PRODL',
0xFF2:'INTCON',0xFF1:'INTCON2',0xFF0:'INTCON3',0xFEF:'INDF0',0xFEE:'POSTINC0',0xFED:'POSTDEC0',
0xFEC:'PREINC0',0xFEB:'PLUSW0',0xFEA:'FSR0H',0xFE9:'FSR0L',0xFE8:'WREG',0xFE7:'INDF1',0xFE6:'POSTINC1',
0xFE5:'POSTDEC1',0xFE4:'PREINC1',0xFE3:'PLUSW1',0xFE2:'FSR1H',0xFE1:'FSR1L',0xFE0:'BSR',0xFDF:'INDF2',
0xFDE:'POSTINC2',0xFDD:'POSTDEC2',0xFDC:'PREINC2',0xFDB:'PLUSW2',0xFDA:'FSR2H',0xFD9:'FSR2L',
0xFD8:'STATUS',0xFD7:'TMR0H',0xFD6:'TMR0L',0xFD5:'T0CON',0xFD3:'OSCCON',0xFD2:'HLVDCON',0xFD1:'WDTCON',
0xFD0:'RCON',0xFCF:'TMR1H',0xFCE:'TMR1L',0xFCD:'T1CON',0xFCC:'TMR2',0xFCB:'PR2',0xFCA:'T2CON',
0xFC9:'SSPBUF',0xFC8:'SSPADD',0xFC7:'SSPSTAT',0xFC6:'SSPCON1',0xFC5:'SSPCON2',0xFC4:'ADRESH',
0xFC3:'ADRESL',0xFC2:'ADCON0',0xFC1:'ADCON1',0xFC0:'ADCON2',0xFBF:'CCPR1H',0xFBE:'CCPR1L',
0xFBD:'CCP1CON',0xFBC:'ECCPR1H',0xFBB:'ECCPR1L',0xFBA:'ECCP1CON',0xFB8:'BAUDCON',0xFB7:'ECCP1DEL',
0xFB6:'ECCP1AS',0xFB5:'CVRCON',0xFB4:'CMCON',0xFB3:'TMR3H',0xFB2:'TMR3L',0xFB1:'T3CON',0xFB0:'SPBRGH',
0xFAF:'SPBRG',0xFAE:'RCREG',0xFAD:'TXREG',0xFAC:'TXSTA',0xFAB:'RCSTA',0xFAA:'EEADRH',0xFA9:'EEADR',
0xFA8:'EEDATA',0xFA7:'EECON2',0xFA6:'EECON1',0xFA5:'IPR3',0xFA4:'PIR3',0xFA3:'PIE3',0xFA2:'IPR2',
0xFA1:'PIR2',0xFA0:'PIE2',0xF9F:'IPR1',0xF9E:'PIR1',0xF9D:'PIE1',0xF9B:'OSCTUNE',0xF96:'TRISE',
0xF95:'TRISD',0xF94:'TRISC',0xF93:'TRISB',0xF92:'TRISA',0xF8D:'LATE',0xF8C:'LATD',0xF8B:'LATC',
0xF8A:'LATB',0xF89:'LATA',0xF84:'PORTE',0xF83:'PORTD',0xF82:'PORTC',0xF81:'PORTB',0xF80:'PORTA',
0xF7F:'SPBRGH?',0xF7E:'BAUDCON?',0xF7D:'F7D',
0xF77:'ECANCON',0xF76:'TXERRCNT',0xF75:'RXERRCNT',0xF74:'COMSTAT',0xF73:'CIOCON',0xF72:'BRGCON3',
0xF71:'BRGCON2',0xF70:'BRGCON1',0xF6F:'CANCON',0xF6E:'CANSTAT',0xF6D:'RXB0D7',0xF6C:'RXB0D6',
0xF6B:'RXB0D5',0xF6A:'RXB0D4',0xF69:'RXB0D3',0xF68:'RXB0D2',0xF67:'RXB0D1',0xF66:'RXB0D0',
0xF65:'RXB0DLC',0xF64:'RXB0EIDL',0xF63:'RXB0EIDH',0xF62:'RXB0SIDL',0xF61:'RXB0SIDH',0xF60:'RXB0CON',
0xF7A:'F7A',0xF79:'F79',0xF78:'F78',
}

def load(fn):
    base=0;mem={}
    for l in open(fn):
        l=l.strip()
        if not l.startswith(':'): continue
        b=bytes.fromhex(l[1:]);n,a,t=b[0],b[1]<<8|b[2],b[3]
        if t==4: base=(b[4]<<8|b[5])<<16
        elif t==0:
            for i in range(n): mem[base+a+i]=b[4+i]
    return mem

def freg(f,a):
    if a==0:
        if f>=0x60: return SFR.get(0xF00|f,'0x%03X'%(0xF00|f))
        return '0x%03X'%f
    return '0x%02X,B'%f

def fname(addr):
    return SFR.get(addr,'0x%03X'%addr)

def dis(mem,pc):
    w=mem.get(pc,0xff)|mem.get(pc+1,0xff)<<8
    w2=mem.get(pc+2,0xff)|mem.get(pc+3,0xff)<<8
    hi=w>>8; f=w&0xff; a=(w>>8)&1; d=(w>>9)&1
    D='W' if d==0 else 'F'
    simple={0:'NOP',3:'SLEEP',4:'CLRWDT',5:'PUSH',6:'POP',7:'DAW',8:'TBLRD*',9:'TBLRD*+',0xA:'TBLRD*-',
            0xB:'TBLRD+*',0xC:'TBLWT*',0xD:'TBLWT*+',0xE:'TBLWT*-',0xF:'TBLWT+*',0x10:'RETFIE',0x11:'RETFIE FAST',
            0x12:'RETURN',0x13:'RETURN FAST',0xFF:'RESET'}
    if hi==0:
        return 2,simple.get(f,'?? %04X'%w),None
    if hi==1: return 2,'MOVLB %d'%(w&0xf),None
    if hi>=8 and hi<=0xF:
        n=['SUBLW','IORLW','XORLW','ANDLW','RETLW','MULLW','MOVLW','ADDLW'][hi-8]
        return 2,'%s 0x%02X'%(n,f),None
    if hi in (2,3): return 2,'MULWF %s'%freg(f,a),None
    if hi>>4 in (0,1,2,3,4,5):
        op=(hi>>2)
        names={1:'DECF',4:'IORWF',5:'ANDWF',6:'XORWF',7:'COMF',8:'ADDWFC',9:'ADDWF',10:'INCF',11:'DECFSZ',
               12:'RRCF',13:'RLCF',14:'SWAPF',15:'INCFSZ',16:'RRNCF',17:'RLNCF',18:'INFSNZ',19:'DCFSNZ',
               20:'MOVF',21:'SUBFWB',22:'SUBWFB',23:'SUBWF'}
        return 2,'%s %s,%s'%(names[op],freg(f,a),D),None
    if hi>>4==6:
        n=['CPFSLT','CPFSEQ','CPFSGT','TSTFSZ','SETF','CLRF','NEGF','MOVWF'][(hi>>1)&7]
        return 2,'%s %s'%(n,freg(f,a)),None
    if hi>>4 in (7,8,9,0xA,0xB):
        n={7:'BTG',8:'BSF',9:'BCF',0xA:'BTFSS',0xB:'BTFSC'}[hi>>4]
        return 2,'%s %s,%d'%(n,freg(f,a),(w>>9)&7),None
    if hi>>4==0xC:
        return 4,'MOVFF %s,%s'%(fname(w&0xfff),fname(w2&0xfff)),None
    if hi>>4==0xD:
        n=w&0x7ff
        if n&0x400: n-=0x800
        t=pc+2+2*n
        return 2,'%s 0x%05X'%('RCALL' if w&0x800 else 'BRA',t),t
    if hi>=0xE0 and hi<=0xE7:
        n=f-256 if f&0x80 else f
        t=pc+2+2*n
        return 2,'%s 0x%05X'%(['BZ','BNZ','BC','BNC','BOV','BNOV','BN','BNN'][hi&7],t),t
    if hi in (0xEC,0xED):
        t=((w2&0xfff)<<8|f)*2
        return 4,'CALL 0x%05X%s'%(t,', FAST' if hi&1 else ''),t
    if hi==0xEF:
        t=((w2&0xfff)<<8|f)*2
        return 4,'GOTO 0x%05X'%t,t
    if hi==0xEE and (f&0xC0)==0:
        return 4,'LFSR %d,0x%03X'%((f>>4)&3,(f&0xf)<<8|(w2&0xff)),None
    if hi>>4==0xF:
        return 2,'NOP (ext %04X)'%w,None
    return 2,'?? %04X'%w,None

if __name__=='__main__':
    mem=load(sys.argv[1])
    start=int(sys.argv[2],16) if len(sys.argv)>2 else 0x1200
    end=int(sys.argv[3],16) if len(sys.argv)>3 else 0x10000
    pc=start
    while pc<end:
        n,txt,t=dis(mem,pc)
        raw=' '.join('%02X'%mem.get(pc+i,0xff) for i in range(n))
        print('%05X: %-12s %s'%(pc,raw,txt))
        pc+=n
