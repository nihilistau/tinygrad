from __future__ import annotations
from typing import Optional, Tuple, Union, List, Dict, Any, ClassVar, Type
import sys, weakref, importlib, inspect, functools, pathlib
from weakref import WeakValueDictionary
from tinygrad.helpers import prod, getenv, DType, dtypes, LazyNumpyArray, flatten
from tinygrad.shape.shapetracker import ShapeTracker, get_contraction
from tinygrad.ops import DeviceBuffer, UnaryOps, BinaryOps, ReduceOps, MovementOps, LoadOps, OpType, LazyOp, get_buffers, get_lazyops, map_buffers
from tinygrad.runtime.ops_cpu import CPUBuffer
from tinygrad.graph import log_op

# lazy can recurse a lot
sys.setrecursionlimit(10000)

OPT = getenv("OPT", 2)
LAZY = getenv("LAZY", 1)

class _Device:
  def __init__(self) -> None:
    self._buffers: List[str] = [x.stem[len("ops_"):].upper() for x in (pathlib.Path(__file__).parent/"runtime").iterdir() if x.stem.startswith("ops_")]
    self.DEFAULT: str = functools.reduce(lambda val, ele: ele if getenv(ele) == 1 else val, self._buffers, "CPU")
  @functools.lru_cache(maxsize=None)  # this class is a singleton, pylint: disable=method-cache-max-size-none
  def __getitem__(self, x:str) -> Type[DeviceBuffer]: return [cls for cname, cls in inspect.getmembers(importlib.import_module(f'tinygrad.runtime.ops_{x.lower()}'), inspect.isclass) if (cname.lower() == x.lower() + "buffer") and x in self._buffers][0]
Device = _Device()

# TODO: movement ops that only change shape are really nops. treat them as such
REMOVE_MOVEMENT_NOPS, MERGE_UNARY_OPS, MERGE_ELEMENTWISE_INTO_REDUCE, SHUFFLE_MOVEMENT_OPS = OPT>=1, OPT>=1, OPT>=1, OPT>=1
MERGE_ELEMENTWISE_OPS, MERGE_ONE_REDUCE_INTO_ELEMENTWISE = OPT>=2, OPT>=2
PUSH_PERMUTES, PUSH_CONTIGUOUS = OPT>=3, OPT>=3
SHUFFLE_PAD_OPS = OPT>=4  # no longer makes wrong outputs since div isn't allowed, but still unadvisable

# **** realize functions ****
def _ast_reduceops(self:LazyBuffer) -> LazyOp:
  # TODO: this can also corealize a binary op after the reduce, not just before
  src = self.op.src[0]
  if MERGE_ELEMENTWISE_INTO_REDUCE and src.realized is None and src.optype == BinaryOps and len(src.children) <= 1:
    src = src.op
  return LazyOp(self.op.op, (src,), self.op.arg)

# this supports late merging an upstream Reduce op and even an Elementwise op above that
def _ast_binaryops(self:LazyBuffer) -> LazyOp:
  real_srcs: Dict[LazyBuffer, Union[None, LazyOp, LazyBuffer]] = {x:None for x in get_buffers(self.op)}
  # NOTE: contiguous does not always mean the same size with SHRINK. this is still mergeable but requires more thought how
  psrcs: List[Tuple[LazyBuffer, LazyBuffer]] = [(k,x) for k,x in zip(real_srcs.keys(), map(get_movementroot_contiguous, real_srcs.keys())) if x.optype == ReduceOps and x.realized is None and prod(k.shape) == prod(x.shape) and len(x.children) <= 1 and len(k.children) <= 1]
  intermediate_shape: Tuple[int, ...] = self.shape
  if len(psrcs) == 1 and MERGE_ONE_REDUCE_INTO_ELEMENTWISE:
    if psrcs[0][1].optype == ReduceOps:
      top = _ast_reduceops(psrcs[0][1])
    real_srcs[psrcs[0][0]] = top
    real_srcs.update({x:x for x in get_buffers(top)})  # the reduce op buffers are not modified

    # if the ReduceOp is followed by a reshape, we push this reshape before all the ElementwiseOp inputs
    if psrcs[0][0].shape != psrcs[0][1].shape:
      intermediate_shape = psrcs[0][1].shape
      assert psrcs[0][0].shape == self.shape, f"shape mismatch {psrcs[0][0].shape} != {self.shape}"

  # reshape all the late ops into the output shape
  # NOTE: these RESHAPEs will return self if they don't change the shape
  for x in real_srcs.keys():
    if real_srcs[x] is None: real_srcs[x] = x.movement_op(MovementOps.RESHAPE, intermediate_shape)
  ast = map_buffers(real_srcs, self.op)
  return LazyOp(MovementOps.RESHAPE, (ast, ), self.shape) if intermediate_shape != self.shape else ast

# **** lazy operations ****

def get_weakop(op:LazyOp) -> LazyOp: return LazyOp(op.op, tuple(get_weakop(x) if isinstance(x, LazyOp) else weakref.ref(x) for x in op.src), op.arg)
def get_single_root(root:LazyBuffer) -> LazyBuffer: return get_single_root(root.op.src[0]) if getattr(root, 'op', None) and len(root.op.src) == 1 else root
def get_movementroot(root:LazyBuffer, allow_contiguous=False) -> LazyBuffer: return get_movementroot(root.op.src[0], allow_contiguous) if root.realized is None and (root.optype == MovementOps or (root.op.op == LoadOps.CONTIGUOUS and allow_contiguous and root.op.src[0].st.contiguous)) else root
def get_movementroot_contiguous(x:LazyBuffer) -> LazyBuffer: return get_movementroot_contiguous(x.op.src[0]) if x.realized is None and x.op.op == LoadOps.CONTIGUOUS else (get_movementroot(x, True) if x.optype == MovementOps and x.st.contiguous else x)

def replace_with_movement_op(y:Union[LazyOp, LazyBuffer], op:MovementOps, arg:Tuple[Any, ...]) -> LazyBuffer:
  if isinstance(y, LazyBuffer): return y.movement_op(op, arg)
  assert y.op in BinaryOps or y.op in UnaryOps
  return elementwise_op(y.op, *[replace_with_movement_op(z, op, arg) for z in y.src])   # type: ignore

def support_weakref(x): return x
@support_weakref  # needed for mypyc, this prevents LazyBuffer from becoming a native class
class LazyBuffer:
  __deletable__ = ('op',)
  lazycache: ClassVar[WeakValueDictionary[Tuple[str, DType, OpType, LazyOp], LazyBuffer]] = WeakValueDictionary()
  def __new__(cls, device:str, shape:Union[ShapeTracker, Tuple[int, ...]], optype:OpType, op:LazyOp, dtype:DType):
    # fromcpu aren't cached
    if optype == LoadOps and op.op == LoadOps.FROMCPU:
      return super().__new__(cls)
    wop = (device, dtype, optype, get_weakop(op))   # NOTE: shape should be deterministic. annoying to cache with the ShapeTracker
    # NOTE: we need "ret" to prevent the new buffer from being immediately deleted
    if wop not in LazyBuffer.lazycache: LazyBuffer.lazycache[wop] = ret = super().__new__(cls)
    else: ret = LazyBuffer.lazycache[wop]
    return ret

  def __init__(self, device:str, shape:Union[ShapeTracker, Tuple[int, ...]], optype:OpType, op:LazyOp, dtype:DType):
    if hasattr(self, 'device'):
      return  # cache hit, we return and don't reinit
    self.st = shape if isinstance(shape, ShapeTracker) else ShapeTracker(tuple(shape))
    self.shape, self.optype, self.op, self.dtype = self.st.shape, optype, op, dtype
    self.realized: Optional[DeviceBuffer] = None
    self.output_buffer: Optional[DeviceBuffer] = None
    self.device, self.dbuffer = device, Device[device]
    # TODO: does children have to be a ref count instead of a set? can a Buffer be a double child?
    self.children: weakref.WeakSet[LazyBuffer] = weakref.WeakSet()
    # NOTE: op should be read only after construction of LazyBuffer
    for x in get_buffers(op): x.children.add(self)
    if not LAZY: self.realize()

  def __repr__(self): return f"<LB {self.shape} {self.dtype} op:{self.op.op if self.realized is None else 'realized'}>"

  # this produces a device buffer
  def realize(self:LazyBuffer, required_device=None) -> DeviceBuffer:
    assert required_device is None or required_device == self.device
    if self.realized is None:
      # get real ops first
      if self.op.op == LoadOps.FROMCPU:
        # resolve LazyNumpyArray
        ast = LazyOp(self.op.op, tuple(), self.op.arg() if isinstance(self.op.arg, LazyNumpyArray) else self.op.arg)
      elif self.op.op == LoadOps.CONTIGUOUS:
        real_src = self.op.src[0].realize(self.device)
        self.realized = real_src.contiguous()
        ast = LazyOp(self.op.op, (real_src, ))
      elif self.op.op == LoadOps.CUSTOM:
        real_srcs = tuple(x.realize(self.device) for x in self.op.src)
        self.realized = self.op.arg(*real_srcs)
        ast = LazyOp(self.op.op, real_srcs)
      elif self.optype == MovementOps:
        src = self.op.src[0]

        # fuse RESHAPE and ReduceOps
        # NOTE: this is sort of a hack for IMAGE, otherwise it shouldn't matter
        if src.realized is None and src.optype == ReduceOps and self.op.op == MovementOps.RESHAPE and len(src.children) <= 1:
          # it's okay to add a RESHAPE to the ast here
          ast = LazyOp(MovementOps.RESHAPE, (_ast_reduceops(src), ), self.op.arg)
        else:
          # movement ops aren't an AST, just run them
          real_src = src.realize(self.device)
          self.realized = real_src.movement_op(self.op.op, self.op.arg)
          ast = LazyOp(self.op.op, (real_src, ))
      elif self.optype == ReduceOps: ast = _ast_reduceops(self)
      elif self.optype == BinaryOps: ast = _ast_binaryops(self)

      # no need to keep the op after realization
      del self.op

      # run the ast if we still have to, and log the op
      if self.realized is None:
        ast = map_buffers({x:x.realize(self.device) for x in get_buffers(ast)}, ast)
        self.realized = self.dbuffer.exec_ast(ast, output_buffer=self.output_buffer)
      log_op(self.realized, ast)

    assert self.realized.shape == self.shape, f"shape mismatch on realize got {self.realized.shape} expected {self.shape}"
    assert isinstance(self.realized, Device[self.device]), f"device mismatch on realized got {type(self.realized)} expected {self.device}"
    assert self.realized.dtype == self.dtype, f"dtype mismatch on realize got {self.realized.dtype} expected {self.dtype}"
    return self.realized

  # NOTE: we have to make a copy of the numpy array here in case the user changes it. expose this? LazyNumpyArray doesn't have this problem
  @staticmethod
  def fromCPU(x, device) -> LazyBuffer: return LazyBuffer(device, x.shape, LoadOps, LazyOp(LoadOps.FROMCPU, tuple(), x.copy()), dtypes.from_np(x))

  # NOTE: we also have to copy the numpy array on the way out...otherwise the underlying Tensor could be freed and use after free. improve this?
  def toCPU(self):
    ret = self.realize().toCPU()
    log_op(CPUBuffer(ret), LazyOp(LoadOps.TOCPU, (self.realized,), None))
    return ret.copy()

  def unary_op(self:LazyBuffer, op:UnaryOps) -> LazyBuffer: return elementwise_op(op, self)
  def binary_op(self:LazyBuffer, op:BinaryOps, y:LazyBuffer) -> LazyBuffer: return elementwise_op(op, self, y)
  def contiguous(self:LazyBuffer) -> LazyBuffer: return LazyBuffer(self.device, self.shape, LoadOps, LazyOp(LoadOps.CONTIGUOUS, (self,)), self.dtype)

  def reduce_op(self:LazyBuffer, op:ReduceOps, new_shape:Tuple[int, ...]) -> LazyBuffer:
    if self.shape == tuple(new_shape): return self
    return LazyBuffer(self.device, new_shape, ReduceOps, LazyOp(op, (self,), new_shape), self.dtype)

  def movement_op(self:LazyBuffer, op:MovementOps, arg:Tuple[Any, ...]) -> LazyBuffer:
    # very instant nop
    if op == MovementOps.RESHAPE and self.shape == arg: return self

    # TODO: look into why that copy is needed
    local_st = ShapeTracker(self.shape).movement_op(op, arg)

    # instant nops
    if local_st.contiguous and self.shape == local_st.shape: return self

    # two ops in a row is one op. merge them if unresolved
    if self.realized is None and self.op.op == op:
      # TODO: why is deleting self from children needed? shouldn't GC do it?
      self.op.src[0].children.discard(self)
      if op in [MovementOps.RESHAPE, MovementOps.EXPAND]: return self.op.src[0].movement_op(op, arg)
      if op == MovementOps.SHRINK: return self.op.src[0].movement_op(op, tuple((b1+b2, b1+e2) for (b1,e1),(b2,e2) in zip(self.op.arg, arg)))
      if op == MovementOps.PERMUTE: return self.op.src[0].movement_op(op, tuple(self.op.arg[i] for i in arg))
      if op == MovementOps.PAD: return self.op.src[0].movement_op(op, tuple((b1+b2, e1+e2) for (b1,e1),(b2,e2) in zip(self.op.arg, arg)))
      if op == MovementOps.STRIDE: return self.op.src[0].movement_op(op, tuple(i*j for i,j in zip(arg, self.op.arg)))

    # push permutes before reduce ops
    if op == MovementOps.PERMUTE and PUSH_PERMUTES and self.realized is None and self.optype == ReduceOps:
      # reduceops have one buffer input, permute it
      narg = tuple(self.op.arg[arg[i]] for i in range(len(arg)))
      src, rop = self.op.src[0], self.op.op
      src.children.discard(self)
      del self  # TODO: why doesn't this delete remove it from the children
      return src.movement_op(op, arg).reduce_op(rop, narg)

    # some permutes are actually just reshapes
    if op == MovementOps.PERMUTE and local_st.contiguous: return self.movement_op(MovementOps.RESHAPE, tuple(self.shape[i] for i in arg))

    # move permutes before expands
    if op == MovementOps.PERMUTE and PUSH_PERMUTES and self.realized is None and self.op.op == MovementOps.EXPAND:
      self.op.src[0].children.discard(self)
      return self.op.src[0].movement_op(MovementOps.PERMUTE, arg).movement_op(MovementOps.EXPAND, tuple(self.op.arg[a] for a in arg))

    # move permutes before reshapes if we can
    if op == MovementOps.PERMUTE and PUSH_PERMUTES and self.realized is None and self.op.op == MovementOps.RESHAPE and isinstance(self.op.src[0], LazyBuffer):
      if shape_idx_groups := get_contraction(self.op.src[0].shape, self.shape):
        self.op.src[0].children.discard(self)   # this changes nothing?
        return self.op.src[0].movement_op(MovementOps.PERMUTE, tuple(flatten(shape_idx_groups[i] for i in arg))) \
          .movement_op(MovementOps.RESHAPE, ShapeTracker(self.st).movement_op(op, arg).shape)

    # if this MovementOp is being applied to a BinaryOp, apply the MovementOp to all the BinaryOp inputs instead. NOTE: UnaryOps is never an OpType
    if SHUFFLE_MOVEMENT_OPS and self.optype == BinaryOps and self.realized is None and len(self.children) == 0 and op != MovementOps.EXPAND and (op != MovementOps.PAD or (SHUFFLE_PAD_OPS and all(x.op != BinaryOps.DIV for x in get_lazyops(self.op)))):
      return replace_with_movement_op(self.op, op, arg)

    # create the buffer
    ret = LazyBuffer(self.device, ShapeTracker(self.st).movement_op(op, arg), MovementOps, LazyOp(op, (self,), arg), self.dtype)

    # if the ShapeTracker becomes contiguous, replace the whole thing with a reshape (or nothing if shapes match)
    # NOTE: if ret is in the cache, it can already be realized
    if REMOVE_MOVEMENT_NOPS and ret.realized is None and self.realized is None and ret.st.contiguous:
      # MovementOps aren't stacked any more, they each have one parent, find the root
      root = get_movementroot(self)
      if root.st.contiguous and root != self and prod(ret.st.shape) == prod(root.shape):
        return root.movement_op(MovementOps.RESHAPE, ret.st.shape)

    return ret

def elementwise_op(op:Union[UnaryOps, BinaryOps], *srcs:LazyBuffer) -> LazyBuffer:
  out_device, out_shape, out_dtype = srcs[0].device, srcs[0].shape, max(x.dtype for x in srcs)

  # push all contiguous to the end of BinaryOps. kernels 198 -> 196
  if PUSH_CONTIGUOUS and any(x.realized is None and x.op.op == LoadOps.CONTIGUOUS and len(x.op.src[0].children) <= 1 for x in srcs):
    new_srcs = []
    for x in srcs:
      if x.realized is None and x.op.op == LoadOps.CONTIGUOUS and len(x.op.src[0].children) <= 1:
        x.op.src[0].children.discard(x)
        new_srcs.append(x.op.src[0])
      else:
        new_srcs.append(x)
    return elementwise_op(op, *new_srcs).contiguous()

  if MERGE_ELEMENTWISE_OPS or (MERGE_UNARY_OPS and len(set(srcs)) == 1):
    # remove the buffers from any (childless) BinaryOps that feed into this
    srcs = tuple(x.op if x.optype == BinaryOps and len(x.children) == 0 and x.realized is None else x for x in srcs)  # type: ignore

  return LazyBuffer(out_device, out_shape, BinaryOps, LazyOp(op, srcs), out_dtype)
