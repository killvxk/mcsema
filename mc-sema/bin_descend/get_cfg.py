##
## Instructions:
## 1) Install python-protobuf for your IDAPython installation. This probably means
## downloading it from https://protobuf.googlecode.com/files/protobuf-2.5.0.tar.gz
## and manually running setup.py
## 2) This script should be run via IDA's batch mode. See the output 
## of --help for more details on the command line options.
##

import idautils
import idaapi
import idc
import sys
import CFG_pb2
from os import path
import os
import argparse
import struct

_DEBUG = False

EXTERNALS = set()
DATA_SEGMENTS = []

RECOVERED_EAS = set()
EMAP = {}
EMAP_DATA = {}

TRAPS = [ 
        idaapi.NN_int3,
        idaapi.NN_icebp,
        ]

CALLS = [
        idaapi.NN_call,
        idaapi.NN_callfi,
        idaapi.NN_callni]

RETS = [
        idaapi.NN_retf,
        idaapi.NN_retfd,
        idaapi.NN_retfq,
        idaapi.NN_retfw,
        idaapi.NN_retn,
        idaapi.NN_retnd,
        idaapi.NN_retnq,
        idaapi.NN_retnw]

COND_BRANCHES = [\
    idaapi.NN_ja,\
    idaapi.NN_jae,\
    idaapi.NN_jb,\
    idaapi.NN_jbe,\
    idaapi.NN_jc,\
    idaapi.NN_jcxz,\
    idaapi.NN_je,\
    idaapi.NN_jecxz,\
    idaapi.NN_jg,\
    idaapi.NN_jge,\
    idaapi.NN_jl,\
    idaapi.NN_jle,\
    idaapi.NN_jna,\
    idaapi.NN_jnae,\
    idaapi.NN_jnb,\
    idaapi.NN_jnbe,\
    idaapi.NN_jnc,\
    idaapi.NN_jne,\
    idaapi.NN_jng,\
    idaapi.NN_jnge,\
    idaapi.NN_jnl,\
    idaapi.NN_jnle,\
    idaapi.NN_jno,\
    idaapi.NN_jnp,\
    idaapi.NN_jns,\
    idaapi.NN_jnz,\
    idaapi.NN_jo,\
    idaapi.NN_jp,\
    idaapi.NN_jpe,\
    idaapi.NN_jpo,\
    idaapi.NN_jrcxz,\
    idaapi.NN_js,\
    idaapi.NN_jz,]

UCOND_BRANCHES = [\
    idaapi.NN_jmp,\
    idaapi.NN_jmpfi,\
    idaapi.NN_jmpni,\
    idaapi.NN_jmpshort]

def DEBUG(s):
    if _DEBUG:
        sys.stdout.write(s)

def isLinkedElf():
    return idc.GetLongPrm(INF_FILETYPE) == idc.FT_ELF and \
        idc.BeginEA() !=0xffffffffL 

def fixExternalName(fn):
    
    if fn in EMAP:
        return fn

    if not isLinkedElf() and fn[0] == '_':
        return fn[1:]

    return fn

def nameInMap(themap, fn):

    return fixExternalName(fn) in themap


def getFromEMAP(fname):

    fixname = fixExternalName(fname)
    return EMAP[fixname]


def doesNotReturn(fname):
    try:
        args, conv, ret = getFromEMAP(fname)
        if ret == "Y":
            return True
    except KeyError, ke:
        raise Exception("Unknown external: " + fname)
    
    return False

def isHlt(ea):
    insn_t = idautils.DecodeInstruction(ea)
    return insn_t.itype in [idaapi.NN_hlt]

def isJmpTable(ea):
    insn_t = idautils.DecodeInstruction(ea)
    is_jmp = insn_t.itype in [idaapi.NN_jmp, 
            idaapi.NN_jmpfi,
            idaapi.NN_jmpni]

    if not is_jmp: return False

    if idaapi.get_switch_info_ex(ea):
        return True

    return False

def addFunction(M, ep):
    F = M.internal_funcs.add()
    F.entry_address = ep

    return F

def entryPointHandler(M, ep, name, args_from_stddef=False):

    EP = M.entries.add()
    EP.entry_name = name
    EP.entry_address = ep

    have_edata = False


    # should we get argument count  
    # calling ocnvention, and return type from std_defs?
    if args_from_stddef:
        try:
            (argc, conv, ret) = getFromEMAP(name)
            have_edata = True
        except KeyError as ke:
            pass

    if not have_edata:
        (argc, conv, ret) = getExportType(name, ep)

    EP.entry_extra.entry_argc = argc
    EP.entry_extra.entry_cconv = conv
    if ret == 'Y':
        EP.entry_extra.does_return = False
    else:
        EP.entry_extra.does_return = True
    
    F = addFunction(M, ep)

    DEBUG("At EP {0}:{1:x}\n".format(name,ep))

    return F

def basicBlockHandler(F, block, blockset, processed_blocks):
    B = F.blocks.add()
    B.base_address = block.startEA
    DEBUG("BB: {0:x}\n".format(block.startEA))

    B.block_follows.extend(block.succs)

    if _DEBUG:
        str_l = ["{0:x}".format(i) for i in block.succs]
        if len(str_l) > 0:
            DEBUG("Successors: {0}\n".format(", ".join(str_l)))

    return B

def readInstructionBytes(inst):
    insn_t = idautils.DecodeInstruction(inst)
    return [idc.Byte(b) for b in xrange(inst, inst+insn_t.size)]
        
def isInternalCode(ea):

    pf = idc.GetFlags(ea)
    return idc.isCode(pf) and not idc.isData(pf)

def isExternalReference(ea):
    # see if this is in an internal or external code ref
    DEBUG("Testing {0:x} for externality\n".format(ea))
    ext_types = [idc.SEG_XTRN]
    seg = idc.SegStart(ea)
    if seg == idc.BADADDR:
        raise Exception("Could not get segment addr for: {0:x}\n".format(ea))

    segtype = idc.GetSegmentAttr(seg, idc.SEGATTR_TYPE)
    if segtype in ext_types:
        return True

    return False

def getFunctionName(ea):
    return idc.GetTrueNameEx(ea,ea)
    
def addInst(block, addr, inst_bytes, true_target=None, false_target=None):
    # check if there is a lock prefix:
    insn_t = idautils.DecodeInstruction(addr)
    if insn_t is not None and (insn_t.auxpref & 0x1) == 0x1:
        # has LOCK
        i_lock = block.insts.add()
        i_lock.inst_addr = addr
        i_lock.inst_bytes = chr(inst_bytes[0])
        i_lock.inst_len = 1

        addr += 1
        inst_bytes = inst_bytes[1:]

    inst = block.insts.add()
    inst.inst_addr = addr
    str_val = "".join([chr(b) for b in inst_bytes])
    inst.inst_bytes = str_val
    inst.inst_len = len(inst_bytes)
    if true_target != None: inst.true_target = true_target
    if false_target != None: inst.false_target = false_target

    return inst

def isConditionalJump(ea):
    insn_t = idautils.DecodeInstruction(ea)
    return insn_t.itype in COND_BRANCHES

def isUnconditionalJump(ea):
    insn_t = idautils.DecodeInstruction(ea)
    return insn_t.itype in UCOND_BRANCHES

def isCall(ea):
    insn_t = idautils.DecodeInstruction(ea)
    return insn_t.itype in CALLS

def isRet(ea):
    insn_t = idautils.DecodeInstruction(ea)
    return insn_t.itype in RETS

def isTrap(ea):
    insn_t = idautils.DecodeInstruction(ea)
    return insn_t.itype in TRAPS

def findRelocOffset(ea, size):
    for i in xrange(ea,ea+size):
        if idc.GetFixupTgtOff(i) != -1:
            return i-ea
    
    return -1

def handleExternalRef(fn):
    # Don't mangle symbols for fully linked ELFs... yet
    if not isLinkedElf():
        if fn.startswith("__imp_"):
            fn = fn[6:]

        if fn.endswith("_0"):
            fn = fn[:-2]

        if fn.startswith("_") and fn not in EMAP:
            fn = fn[1:]

        if fn.startswith("@") and fn not in EMAP:
            fn = fn[1:]

        if '@' in fn:
            fn = fn[:fn.find('@')]

    EXTERNALS.add(fn)
    return fn

def isInData(start_ea, end_ea):
    for (start,end) in DATA_SEGMENTS:
        if start_ea >= start and start_ea < end:
            DEBUG("{0:x} > {1:x}\n".format(start_ea, start))
            if end_ea <= end:
                return True
            else:
                DEBUG("{0:x} NOT <= {1:x}\n".format(end_ea, end))
                DEBUG("{0:x}-{1:x} overlaps with: {2:x}-{3:x}\n".format(start_ea, end_ea, start, end))
                raise Exception("Overlapping data segments!")
        else:
            if end_ea > start and end_ea <= end:
                DEBUG("Overlaps with: {0:x}-{1:x}\n".format(start, end))
                raise Exception("Overlapping data segments!")

    return False

def isExternalData(fn):
    indata = fn in EMAP_DATA
    incode = fn in EMAP

    if indata and not incode:
        return True
    elif indata and incode:
        raise Exception("Symbol "+fn+" defined as both code and data!")
    else:
        return False


def handleJmpTable(I, inst, new_eas):
    si = idaapi.get_switch_info_ex(inst)
    jsize = si.get_jtable_element_size()
    jstart = si.jumps

    # only handle size 4 cases
    if jsize != 4:
        raise Exception("Jump table size not 4!")
        return

    DEBUG("\tJMPTable Start: {0:x}\n".format(jstart))
    I.jump_table.zero_offset = 0
    i = 0
    je = idc.GetFixupTgtOff(jstart+i*jsize)
    while je != -1:
        I.jump_table.table_entries.append(je)
        if je not in RECOVERED_EAS: 
            new_eas.add(je)
        DEBUG("\t\tAdding JMPTable {0}: {1:x}\n".format( i, je))
        i += 1
        je = idc.GetFixupTgtOff(jstart+i*jsize)

def isElfThunk(ea):
    if not isLinkedElf():
        return False, None


    if isUnconditionalJump(ea):
        have_ext_ref = False
        for cref in idautils.CodeRefsFrom(ea, 0):
            if isExternalReference(cref):
                have_ext_ref = True
                break

        if have_ext_ref:
            fn = getFunctionName(cref)
            return True, fn

    return False, None

def instructionHandler(M, B, inst, new_eas):
    insn_t = idautils.DecodeInstruction(inst)
    if not insn_t:
        # handle jumps after noreturn functions
        if idc.Byte(inst) == 0xCC:
            I = addInst(B, inst, [0xCC])
            return I, True
        else:
            raise Exception("Cannot read instruction at: {0:x}".format(inst))

    # skip HLTs -- they are privileged, and are used in ELFs after a noreturn call
    if isHlt(inst):
        return None, False

    DEBUG("\t\tinst: {0}\n".format(idc.GetDisasm(inst)))
    inst_bytes = readInstructionBytes(inst)
    DEBUG("\t\tBytes: {0}\n".format(inst_bytes))

    I = addInst(B, inst, inst_bytes)

    if isJmpTable(inst):
        handleJmpTable(I, inst, new_eas)
        return I, False

    #check for code refs from here
    crefs = []
    for cref in idautils.CodeRefsFrom(inst, 0):
        crefs.append(cref)
        fn = getFunctionName(cref)
        if isCall(inst):

            elfy, fn_replace = isElfThunk(cref) 
            if elfy:
                fn = fn_replace

            if isExternalReference(cref) or elfy:
                fn = handleExternalRef(fn)
                I.ext_call_name = fn 
                DEBUG("EXTERNAL CALL: {0}\n".format(fn))

                if doesNotReturn(fn):
                    return I, True
            else:
                I.call_target = cref

                if cref not in RECOVERED_EAS: 
                    new_eas.add(cref)

                DEBUG("INTERNAL CALL: {0}\n".format(fn))
        elif isUnconditionalJump(inst):
            if isExternalReference(cref):
                fn = handleExternalRef(fn)
                I.ext_call_name = fn 
                DEBUG("EXTERNAL JMP: {0}\n".format(fn))

                if doesNotReturn(fn):
                    DEBUG("Nonreturn JMP\n")
                    return I, True
            else:
                DEBUG("INTERNAL JMP: {0:x}\n".format(cref))
                I.true_target = cref

    #true: jump to where we have a code-ref
    #false: continue as we were
    if isConditionalJump(inst):
        I.true_target = crefs[0]
        I.false_target = inst+len(inst_bytes)
        return I, False

    relo_off = findRelocOffset(inst, len(inst_bytes))
    if relo_off != -1:
        I.reloc_offset = relo_off

    for dref in idautils.DataRefsFrom(inst):
        if dref in crefs:
            continue

        if inValidSegment(dref): 
            if isExternalReference(dref):
                fn = getFunctionName(dref)

                fn = handleExternalRef(fn)
                if isExternalData(fn):
                    I.ext_data_name = fn
                    sys.stdout.write("EXTERNAL DATA REF FROM {0:x} to {1}\n".format(inst, fn))
                else:
                    I.ext_call_name = fn 
                    sys.stdout.write("EXTERNAL CODE REF FROM {0:x} to {1}\n".format(inst, fn))

            elif isInternalCode(dref):
                I.call_target = dref
                if dref not in RECOVERED_EAS: 
                    new_eas.add(dref)
            else:
                dref_size = idc.ItemSize(dref)
                DEBUG("\t\tData Ref: {0:x}, size: {1}\n".format(
                    dref, dref_size))
                I.data_offset = handleDataRelocation(M, dref, new_eas)
        else:
            DEBUG("Data not in valid segment {0:x}\n".format(dref))


    return I, False

def parseDefsFile(df):
    emap = {}
    emap_data = {}
    for l in df.readlines():
        #skip comments
        if l[0] == "#":
            continue
         
        l = l.strip()
        
        if l.startswith('DATA:') :
            # process as data
            (marker, symname, dsize) = l.split()
            emap_data[symname] = int(dsize)
        else:

            (fname, args, conv, ret) = l.split()

            if conv == "C":
                realconv = CFG_pb2.ExternalFunction.CallerCleanup
            elif conv == "E":
                realconv = CFG_pb2.ExternalFunction.CalleeCleanup
            elif conv == "F":
                realconv = CFG_pb2.ExternalFunction.FastCall
            else:
                raise Exception("Unknown calling convention:"+conv)

            if ret not in ['Y', 'N']:
                raise Exception("Unknown return type:"+ret)

            emap[fname] = (int(args), realconv, ret)

    
    df.close()

    return emap, emap_data

def processExternalFunction(M, fn):

    args, conv, ret = getFromEMAP(fn)

    extfn = M.external_funcs.add()
    extfn.symbol_name = fn
    extfn.calling_convention = conv
    extfn.argument_count = args
    if ret == 'N':
        extfn.has_return = True
        extfn.no_return = False
    else:
        extfn.has_return = False
        extfn.no_return = True

def processExternalData(M, dt):

    data_size = EMAP_DATA[dt]

    extdt = M.external_data.add()
    extdt.symbol_name = dt 
    extdt.data_size = data_size

def processExternals(M):

    for fn in EXTERNALS:

        fn = fixExternalName(fn)

        if nameInMap(EMAP, fn):
            processExternalFunction(M, fn)
        elif nameInMap(EMAP_DATA, fn):
            processExternalData(M, fn)
        else:
            sys.stderr.write("UNKNOWN API: {0}\n".format(fn))

def readBytesSlowly(start, end):
    bytestr = ""
    for i in xrange(start, end):
        if idc.hasValue(idc.GetFlags(i)):
            bt = idc.Byte(i)
            bytestr += chr(bt)
        else:
            #virtual size may be bigger than size on disk
            #pad with nulls
            #DEBUG("Failed on {0:x}\n".format(i))
            bytestr += "\x00"
    return bytestr

def handleDataRelocation(M, dref, new_eas):
    dref_size = idc.ItemSize(dref)
    if not isInData(dref, dref+dref_size):
        return dref + addDataSegment(M, dref, dref+dref_size, new_eas)
    else:
        return dref

def resolveRelocation(ea):
    rtype = idc.GetFixupTgtType(ea) 
    if rtype == idc.FIXUP_OFF32:
        bytestr = readBytesSlowly(ea, ea+4);
        relocVal = struct.unpack("<L", bytestr)[0]
        return relocVal
    elif rtype == -1:
        raise Exception("No relocation type at ea: {:x}".format(ea))
    else:
        return idc.GetFixupTgtOff(ea)

def processRelocationsInData(M, D, start, end, new_eas, seg_offset):

    if start == 0:
        start = 1

    i = idc.GetNextFixupEA(start-1)

    while i < end and i != idc.BADADDR:

        pointsto = resolveRelocation(i)
        fn = getFunctionName(i)
        DEBUG("{0:x} Found reloc to: {1:x}\n".format(i, pointsto))

        if not isExternalReference(pointsto):
            pf = idc.GetFlags(pointsto)

            DS = D.symbols.add()
            DS.base_address = i+seg_offset

            if idc.isCode(pf):
                DS.symbol_name = "sub_"+hex(pointsto)
                DEBUG("Code Ref: {0:x}!\n".format(pointsto))

                if pointsto not in RECOVERED_EAS:
                    new_eas.add(pointsto)

            elif idc.isData(pf):
                pointsto = handleDataRelocation(M, pointsto, new_eas)
                DS.symbol_name = "dta_"+hex(pointsto)
                DEBUG("Data Ref!\n")
            else:
                pointsto = handleDataRelocation(M, pointsto, new_eas)
                DS.symbol_name = "dta_"+hex(pointsto)
                DEBUG("UNKNOWN Ref, assuming data\n")


        i = idc.GetNextFixupEA(i)

def inValidSegment(ea):
    if idc.SegStart(ea) == idc.BADADDR:
        return False

    return True

def findFreeData():

    max_end = 0
    for (start, end) in DATA_SEGMENTS:
        if end > max_end:
            max_end = end

    return max_end+4

def addDataSegment(M, start, end, new_eas):
    if end < start:
        raise Exception("Start must be before end")

    seg = idaapi.getseg(start)

    if not seg:
        raise Exception("Data must be in a valid segment")

    # if this is in an executalbe region,
    # move it to a data section
    seg_offset = 0
    need_move = (seg.perm & idaapi.SEGPERM_EXEC) != 0
    if need_move:
        free_data = findFreeData()
        seg_offset = free_data - start
        DEBUG("Data Segment {0:x} moved to: {1:x}\n".format(start, start+seg_offset))

    D = M.internal_data.add()
    D.base_address = start+seg_offset

    SEGPERM_WRITE = 2
    
    if (seg.perm & SEGPERM_WRITE) == 0:
        D.read_only = True
    else:
        D.read_only = False

    #D.data = idaapi.get_many_bytes(start, end-start)
    D.data = readBytesSlowly(start, end)

    DATA_SEGMENTS.append( (start+seg_offset,end+seg_offset) )

    processRelocationsInData(M, D, start, end, new_eas, seg_offset)

    DEBUG("Adding data seg: {0}: {1}-{2}\n".format( 
        idc.SegName(start),
        hex(start+seg_offset),
        hex(end+seg_offset)))

    return seg_offset

def processDataSegments(M, new_eas):
    for n in xrange(idaapi.get_segm_qty()):
        seg = idaapi.getnseg(n)
        ea = seg.startEA
        segtype = idc.GetSegmentAttr(ea, idc.SEGATTR_TYPE)
        if segtype in [idc.SEG_DATA, idc.SEG_BSS]:
            start = idc.SegStart(ea)
            end = idc.SegEnd(ea)
            addDataSegment(M, start, end, new_eas)

def recoverFunctionFromSet(M, F, blockset, new_eas):
    processed_blocks = set()

    while len(blockset) > 0:
        block = blockset.pop()

        if block.startEA == block.endEA:
            sys.stdout.write("Zero sized block: {0:x}\n".format(block.startEA))

        if block.startEA in processed_blocks:
            raise Exception("Attempting to add same block twice: {0:x}".format(block.startEA))

        processed_blocks.add(block.startEA)

        B = basicBlockHandler(F, block, blockset, processed_blocks)
        for head in idautils.Heads(block.startEA, block.endEA):
            I, endBlock = instructionHandler(M, B, head, new_eas)
            # sometimes there is junk after a terminator due to off-by-ones in
            # IDAPython. Ignore them.
            if endBlock or isRet(head) or isUnconditionalJump(head) or isTrap(head):
                break

def recoverFunction(M, F, fnea, new_eas):
    blockset = getFunctionBlocks(fnea)
    recoverFunctionFromSet(M, F, blockset, new_eas)

class Block:
    def __init__(self, startEA):
        self.startEA = startEA
        self.endEA = startEA
        self.succs = []

def recoverBlock(startEA):
    b = Block(startEA)
    curEA = startEA

    while True:
        insn_t = idautils.DecodeInstruction(curEA)
        if insn_t is None:
            if idc.Byte(curEA) == 0xCC:
                b.endEA = curEA+1
                return b
            else:
                sys.stdout.write("WARNING: Couldn't decode insn at: {0:x}. Ending block.\n".format(curEA))
                b.endEA = curEA
                return b

        nextEA = curEA+insn_t.size

        crefs = idautils.CodeRefsFrom(curEA, 1)

        # get curEA follows
        follows = [cref for cref in crefs]

        if follows == [nextEA] or isCall(curEA):
            # there is only one following branch, to the next instruction
            # check if this is a JMP 0; in that case, make a new block
            if isUnconditionalJump(curEA):
                b.endEA = nextEA
                for f in follows:
                    # do not decode external code refs
                    if not isExternalReference(f):
                        b.succs.append(f)
                return b

            # if its not JMP 0, add next instruction to current block
            curEA = nextEA
        # check if we need to make a new block
        elif len(follows) == 0:
            # this is a ret, no follows
            b.endEA = nextEA
            return b
        else:
            # this block has several follow blocks
            b.endEA = nextEA
            for f in follows:
                # do not decode external code refs
                if not isExternalReference(f):
                    b.succs.append(f)
            return b

def getFunctionBlocks(startea):
    to_recover = [startea]
    
    blocks = {}

    while len(to_recover) > 0:
        # get new block start to recover
        bstart = to_recover.pop()
        # recover the block
        newb = recoverBlock(bstart)
        # save to our recovered block list
        blocks[newb.startEA] = newb
        # add new workers
        for fba in newb.succs:
            if fba not in blocks:
                to_recover.append(fba)

    rv = []
    # easier to debug
    for k in sorted(blocks.keys()):
        rv.append(blocks[k])

    return rv

def recoverCfg(to_recover, outf, exports_are_apis=False):
    M = CFG_pb2.Module()
    M.module_name = idc.GetInputFile()
    DEBUG("PROCESSING: {0}\n".format(M.module_name))

    our_entries = []
    entrypoints = idautils.Entries()
    exports = {}
    for index,ordinal,exp_ea, exp_name in entrypoints:
        exports[exp_name] = exp_ea
        
    new_eas = set()
    processDataSegments(M, new_eas)

    for name in to_recover:

        if name in exports:
            ea = exports[name]
        else:
            ea = idc.LocByName(name)
            if ea == idc.BADADDR:
                raise Exception("Could not locate entry symbol: {0}".format(name))

        fwdname = isFwdExport(name, ea)

        if fwdname is not None:
            sys.stdout.write("Skipping fwd export {0} : {1}\n".format(name, fwdname))
            continue

        if not isInternalCode(ea):
            sys.stdout.write("Export {0} does not point to code; skipping\n".format(name))
            continue
            
        our_entries.append( (name, ea) )

    recovered_fns = 0

    # process main entry points
    for fname, fea in our_entries:

        sys.stdout.write("Recovering: {0}\n".format(fname))

        F = entryPointHandler(M, fea, fname, exports_are_apis)

        RECOVERED_EAS.add(fea)
        recoverFunction(M, F, fea, new_eas)

        recovered_fns += 1

    # process subfunctions
    new_eas.difference_update(RECOVERED_EAS)

    while len(new_eas) > 0:
        cur_ea = new_eas.pop()
        if not isInternalCode(cur_ea):
            raise Exception("Function EA not code: {0:x}".format(cur_ea))

        F = addFunction(M, cur_ea)
        sys.stdout.write("Recovering: {0}\n".format(hex(cur_ea)))
        RECOVERED_EAS.add(cur_ea)

        recoverFunction(M, F, cur_ea, new_eas)

        recovered_fns += 1

    if recovered_fns == 0:
        sys.stderr.write("COULD NOT RECOVER ANY FUNCTIONS\n")
        return

    mypath = path.dirname(__file__)
    processExternals(M)

    outf.write(M.SerializeToString())
    outf.close()

    sys.stdout.write("Recovered {0} functions.\n".format(recovered_fns))
    sys.stdout.write("Saving to: {0}\n".format(outf.name))

def isFwdExport(iname, ea):
    l = ea
    if l == idc.BADADDR:
        raise Exception("Cannot find addr for: " + iname)

    pf = idc.GetFlags(l)

    if not idc.isCode(pf) and idc.isData(pf):
        sz = idc.ItemSize(l)
        iname = idaapi.get_many_bytes(l, sz-1)
        return iname

    return None

def writeDriverLine(batfile, name, ea):

    args, conv, ret = getExportType(name, ea)

    retstr = "return"
    if ret == "Y": retstr = "noreturn"

    batfile.write(" -driver=driver_{0},{0},{1},{2}".format(name, args, retstr))

def generateBatFile(batname, eps):
    infile = idc.GetInputFile()
    batfile = open(batname, 'wb')
    batheader = """
    @echo off
    set /p LLVM_PATH= < LLVM_PATH
    set /p CFG_TO_BC_PATH= < CFG_TO_BC_PATH

    set CFG_TO_BC=%CFG_TO_BC_PATH%\cfg_to_bc.exe
    set OPT=%LLVM_PATH%\opt.exe
    set LLC=%LLVM_PATH%\llc.exe
    REM
    REM
    echo Making API Import libs...
    cmd /c makelibs.bat > NUL
    echo Converting CFG to Bitcode
    del {}.bc 2>NUL
    """.format(infile)

    batfile.write(batheader)
    batfile.write("%CFG_TO_BC% ")
    batfile.write("-ignore-unsupported=true -i={0}_ida.cfg -o={0}.bc\n".format(infile))
    batfile.write("\n")
    batfile.write(" echo Optimizing Bitcode\n")
    batfile.write("%OPT% ")
    batfile.write("-O3 -o {0}_opt.bc {0}.bc\n".format(infile))
    batfile.write("echo Creating .obj\n")
    batfile.write("del kernel32.dll.obj 2>NUL\n")
    batfile.write("%LLC% ")
    batfile.write("-O3 -filetype=obj -o {0}.obj {0}_opt.bc\n".format(infile))
    batfile.write("echo Building export stub\n")
    batfile.write("cl /c {0}_exportstub.c \n".format(infile))
    batfile.write("REM Below is a compilation template. You need to uncomment it to build.\n")
    batfile.write("REM and add some .lib files to the line as well.\n")
    batfile.write("REM \n")
    batfile.write("REM link /NODEFAULTLIB /ENTRY:export_DllEntryPoint /DLL /DEF:{0}.def /OUT:{0} {0}.obj {0}_exportstub.obj msvcrt.lib *.lib \n".format(infile))
    batfile.write("echo Uncomment lines to attempt linking to a DLL\n")
    batfile.close()

def parseTypeString(typestr, ea):

    if "__stdcall" in typestr:
        conv = CFG_pb2.ExternalFunction.CalleeCleanup
    elif "__cdecl" in typestr:
        conv = CFG_pb2.ExternalFunction.CallerCleanup
    elif "__fastcall" in typestr:
        conv = CFG_pb2.ExternalFunction.FastCall
    elif "__usercall" in typestr:
        # do not handle this for now
        return (0, CFG_pb2.ExternalFunction.CalleeCleanup, "N")
    else:
        raise Exception("Could not parse function type:"+typestr)

    fn = idaapi.get_func(ea)
    if fn is None:
        raise Exception("Could not get function args for: {0:x}".format(ea))
    args = fn.argsize / 4

    ret = 'N'

    return args, conv, ret

def getExportType(name, ep):
    try:
        DEBUG("Processing export name: {} at: {:x}\n".format(name, ep))
        args, conv, ret = getFromEMAP(name)
    except KeyError as ke:
        tp = idc.GetType(ep);
        if tp is None or "__" not in tp: 
            #raise Exception("Cannot determine type of function: {0} at: {1:x}".format(name, ep))
            sys.stdout.write("WARNING: Cannot determine type of function: {0} at: {1:x}".format(name, ep))
            return (0, CFG_pb2.ExternalFunction.CalleeCleanup, "N")

        return parseTypeString(tp, ep)

    return args, conv, ret

def generateDefFile(defname, eps):
    deffile = open(defname, 'wb')
    deffile.write("EXPORTS\n")
    entrypoints = idautils.Entries()

    for ep_tuple in entrypoints:
        (index, ordinal, ea, name) = ep_tuple

        if name not in eps:
            continue

        fwdname = isFwdExport(name, ea)
        if fwdname is not None:
            deffile.write("{0}={1}\n".format(name, fwdname))
        else:
            args, conv, ret = getExportType(name, ea)

            if conv == CFG_pb2.ExternalFunction.CallerCleanup:
                decor_name = "_export_{0}".format(name)
            elif conv == CFG_pb2.ExternalFunction.CalleeCleanup:
                decor_name = "_export_{0}@{1}".format(name, args*4)
            elif conv == CFG_pb2.ExternalFunction.FastCall:
                decor_name = "@export_{0}@{1}".format(name, args*4)
            else:
                raise Exception("Unknown calling convention: " + str(conv))

            deffile.write("{0}={1}\n".format(name, decor_name))

    deffile.close()

def makeArgStr(name, declaration):

    argstr = "void"
    args, conv, ret = getFromEMAP(name)

    # return blank string for void calls
    if not declaration and args == 0:
        return ""

    if declaration:
        joinstr = "int a"
    else:
        joinstr = "a"

    argl = [joinstr+str(a) for a in xrange(args)]

    if args > 0:
        argstr = ", ".join(argl)

    return argstr

def generateExportStub(cname, eps):
    cfile = open(cname, 'wb')
    entrypoints = idautils.Entries()

    for ep_tuple in entrypoints:
        (index, ordinal, ea, name) = ep_tuple

        if name not in eps:
            continue

        fwdname = isFwdExport(name, ea)
        if fwdname is not None:
            continue
        else:
            args, conv, ret =  getExportType(name, ea)

            if conv == CFG_pb2.ExternalFunction.CallerCleanup:
                convstr = "__cdecl"
            elif conv == CFG_pb2.ExternalFunction.CalleeCleanup:
                convstr = "__stdcall"
            elif conv == CFG_pb2.ExternalFunction.FastCall:
                convstr = "__fastcall"
            else:
                raise Exception("Unknown calling convention")

            declargs = makeArgStr(name, declaration=True)
            callargs = makeArgStr(name, declaration=False)

            cfile.write("extern int {2} driver_{0}({1});\n".format(name, declargs, convstr))
            cfile.write("int {3} export_{0}({1}) {{ return driver_{0}({2}); }} \n".format(
                name, declargs, callargs, convstr))
            cfile.write("\n")

    cfile.close()

def getAllExports() :
    entrypoints = idautils.Entries()
    to_recover = set()
    # recover every entry point
    for ep_tuple in entrypoints:
        (index, ordinal, ea, name) = ep_tuple
        to_recover.add(name)

    return to_recover 

    
if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", 
        help="Indicate the script is running in batch mode",
        action="store_true",
        default=False)

    parser.add_argument("--entry-symbol", nargs='*', help="Symbol(s) to start disassembling from")

    parser.add_argument("-o", "--output", type=argparse.FileType('wb'),
        default=None,
        help="The output control flow graph recovered from this file")

    parser.add_argument("-s", "--std-defs", nargs='*', type=argparse.FileType('r'),
        default=None,
        help="std_defs file: definitions and calling conventions of imported functions and data"
        )
    
    parser.add_argument("-e", "--exports-to-lift", type=argparse.FileType('r'),
        default=None,
        help="A file containing a exported functions to lift, one per line. If not specified, all exports will be lifted."
        )
    parser.add_argument("--make-export-stubs", action="store_true",
        default=False,
        help="Generate a .bat/.c/.def combination to provide export symbols. Use this if you're lifting a DLL and want to re-export the same symbols"
        )
    parser.add_argument("--exports-are-apis", action="store_true",
        default=False,
        help="Exported functions are defined in std_defs. Useful when lifting DLLs"
        )
    parser.add_argument("-d", "--debug", action="store_true",
        default=False,
        help="Enable verbose debugging mode"
        )
                        
    args = parser.parse_args(args=idc.ARGV[1:])

    if args.debug:
        _DEBUG = True

    # for batch mode: ensure IDA is done processing
    if args.batch:
        analysis_flags = idc.GetShortPrm(idc.INF_START_AF)
        analysis_flags &= ~idc.AF_IMMOFF
        # turn off "automatically make offset" heuristic
        idc.SetShortPrm(idc.INF_START_AF, analysis_flags)
        idaapi.autoWait()

    myname = idc.GetInputFile()
    mypath = path.dirname(__file__)

    EMAP = {}
    EMAP_DATA = {}

    if args.std_defs:
        for defsfile in args.std_defs:
            sys.stdout.write("Loading Standard Definitions file: {0}\n".format(defsfile.name))
            em_update, emd_update = parseDefsFile(defsfile)
            EMAP.update(em_update)
            EMAP_DATA.update(emd_update)

    if args.output:
        outpath = os.path.dirname(args.output.name)
    else:
        outpath =  os.path.join(mypath, myname)
        try:
            os.mkdir(outpath)
        except:
            pass

    eps = []
    try:
        if args.exports_to_lift: 
            eps = args.exports_to_lift.readlines()
        elif args.entry_symbol is None:
            eps = getAllExports()

        eps = [ep.strip() for ep in eps]

    except IOError as e:
        sys.stdout.write("Could not open file of exports to lift. See source for details\n")
        sys.exit(-1)

    if args.entry_symbol:
        eps.extend(args.entry_symbol)

    assert len(eps) > 0, "Need to have at least one entry point to lift"

    sys.stdout.write("Will lift {0} exports\n".format(len(eps)))
    if args.make_export_stubs:
        sys.stdout.write("Generating export stubs...\n");

        outdef = path.join(outpath, "{0}.def".format(myname))
        sys.stdout.write("Output .DEF file: {0}\n".format(outdef))
        generateDefFile(outdef, eps)

        outstub = path.join(outpath, "{0}_exportstub.c".format(myname))
        sys.stdout.write("Output export stub file: {0}\n".format(outstub))
        generateExportStub(outstub, eps)

        outbat = path.join(outpath, "{0}.bat".format(myname))
        sys.stdout.write("Output build .BAT: {0}\n".format(outbat))
        generateBatFile(outbat, eps)


    if args.output:
        outf = args.output
    else:
        cfgname = path.join(outpath, myname + "_ida.cfg")
        cfgpath = path.join(outpath, cfgname)
        outf = open(cfgpath, 'wb')

    sys.stdout.write("CFG Output File file: {0}\n".format(outf.name))
    recoverCfg(eps, outf, args.exports_are_apis)

    #for batch mode: exit IDA when done
    if args.batch:
        idc.Exit(0)
    
