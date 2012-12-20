"""
Execute raw graph to ATerm after inference but before evaluation.
"""

import string
import numpy as np

from collections import namedtuple

from blaze.datashape import datashape
from blaze.datashape.coretypes import DataShape
from blaze.byteproto import CONTIGUOUS, READ

from blaze.expr import paterm
from blaze.expr.paterm import AAppl, ATerm, AAnnotation, AString, AInt, AFloat
from blaze.expr.visitor import MroVisitor

#------------------------------------------------------------------------
# Plans
#------------------------------------------------------------------------

# Annotate with simple_type() which will pull the simple type of
# the App, Op, or Literal node before we even hit eval(). This is
# a stop gap measure because right we're protoyping this without
# having the types annotated on the graph that we're using for
# Numba code generation. It's still probably a good idea to have
# this knowledge available if we have it though!

def annotate_dshape(ds):
    """
    Convert a datashape instance into Aterm annotation

    >>> ds = dshape('2, 2, int32')
    >>> anno = dshape_anno(ds)
    dshape("2, 2, int32")
    >>> type(anno)
    <class 'AAppl'>
    """

    assert isinstance(ds, DataShape)
    return AAppl(ATerm('dshape'), [AString(str(ds))])

def annotation(graph, *metadata):
    # Metadata holds a reference to the graph node, not really
    # what we want but fine for now...
    metadata = (id(graph),) + metadata

    # was originally .datashape but this is a reserved attribute
    # so moved to a new simple_type() method that wraps around
    # promote()
    anno = annotate_dshape(graph.datashape)
    annotation = AAnnotation(anno, metadata)
    return annotation

def get_datashape(term):
    "Assemble datashape from aterm dshape"
    type = term.annotation.ty

    args = []
    for arg in type.args:
        if isinstance(arg, paterm.AInt):
            args.append(arg.n)
        elif isinstance(arg, paterm.AString):
            args.append(arg.s)
        else:
            raise NotImplementedError

    return datashape(*args)

#------------------------------------------------------------------------
# ATerm -> Instructions
#------------------------------------------------------------------------

class Constant(object):
    def __init__(self, n):
        self.n = n
    def __repr__(self):
        return 'const(%s)' % self.n

class Var(object):
    def __init__(self, key):
        self.key = key
    def __repr__(self):
        return self.key

class Instruction(object):
    def __init__(self, fn, datashape, args=None, lhs=None, fillvalue=None):
        """ %lhs = fn{props}(arguments) """

        self.fn = fn
        self.lhs = lhs
        self.args = args or []

        # Value to initialize the LHS with (applicable for reductions)
        self.fillvalue = fillvalue

        self.datashape = datashape

    def execute(self, operands, lhs):
        return self.fn(operands, lhs)

    def __repr__(self):
        # with output types
        rhs = '%s(%s)' % (self.fn, ' '.join(map(repr, self.args)))
        if self.lhs:
            return '%s = %s' % (self.lhs, rhs)
        # purely side effectful
        else:
            return rhs


# TODO: naive constant folding

class InstructionGen(MroVisitor):
    """ Map ATerm into linear instructions, unlike ATerm this
    does not preserve the information contained in the expression
    graph, information is discarded.

    Maintains a stack as the nodes are visited, the instructions
    for the innermost term are top on the stack. The temporaries
    are mapped through the vartable.

    ::

        a + b * c

    ::

        instructions = [
            %3 = <ufunc 'multiply'> %1 %2,
            %4 = <ufunc 'add'> %0 %3
        ]

        vartable = {
            Array(){dshape("2, 2, int32"),54490464}   : '%0',
            Array(){dshape("2, 2, float32"),54490176} : '%1',
            Array(){dshape("2, 2, int32"),54491184}   : '%2',
            ...
        }

    """

    def __init__(self, executors, have_numbapro):
        self.executors = executors
        self.numbapro = have_numbapro

        self.n = 0
        self._vartable = {}
        self._instructions = []

    def result(self):
        return self._instructions

    @property
    def vars(self):
        return self._vartable

    @property
    def symbols(self):
        return dict((name, term) for term, name in self._vartable.iteritems())

    def var(self, term):
        key = ('%' + str(self.n))
        self._vartable[term] = key
        self.n += 1
        return key

    def AAppl(self, term):
        label = term.spine.label

        if label == 'Arithmetic':
            return self._Arithmetic(term)
        elif label == 'Array':
            return self._Array(term)
        elif label == 'Slice':
            return self._Slice(term)
        elif label == 'Assign':
            return self._Assign(term)
        elif label == 'Executor':
            return self._Executor(term)
        else:
            raise NotImplementedError(term)

    def _Arithmetic(self, term):
        # All the function signatures are of the form
        #
        #     Add(a,b)
        #
        # But the aterm expression for LLVM is expected to be
        #
        #     Arithmetic(Add, ...)
        #
        # so we do this ugly hack to get the signature back to
        # standard form

        # -- hack --
        op   = term.args[0]
        args = term.args[1:]
        normal_term = AAppl(ATerm(op), args)
        # --

        assert isinstance(op, ATerm)
        label = op.label

        # Find us implementation for execution
        # Returns either a ExternalF ( reference to a external C
        # library ) or a PythonF, a Python callable. These can be
        # anything, numpy ufuncs, numexpr, pandas, cmath whatever
        from blaze.rts.funcs import lookup

        # visit the innermost arguments, push those arguments on
        # the instruction list first
        self.visit(args)

        fn, cost = lookup(normal_term)
        fargs = [self._vartable[a] for a in args]

        # push the temporary for the result in the vartable
        key = self.var(term)

        # build the instruction & push it on the stack
        inst = Instruction(str(fn.fn), get_datashape(term), fargs, lhs=key)
        self._instructions.append(inst)

    def _Array(self, term):
        key = self.var(term)
        return Var(key)

    def _Assign(self, term):
        pass

    def _Slice(self, term):
        pass

    def _Executor(self, term):
        executor_id, backend, has_lhs = term.annotation.meta
        has_lhs = has_lhs.label
        executor = self.executors[executor_id.label]

        self.visit(term.args)

        fargs = [self._vartable[a] for a in term.args]

        if has_lhs:
            fargs, lhs = fargs[:-1], fargs[-1]
        else:
            lhs = None

        # build the instruction & push it on the stack
        inst = Instruction(executor, get_datashape(term), fargs, lhs=lhs)
        self._instructions.append(inst)

    def AInt(self, term):
        self._vartable[term] = Constant(term.n)
        return

    def AFloat(self, term):
        self._vartable[term] = Constant(term.n)
        return

    def ATerm(self, term):
        return

#------------------------------------------------------------------------
# Graph -> ATerm
#------------------------------------------------------------------------

class BlazeVisitor(MroVisitor):
    """ Map Blaze graph objects into ATerm """

    def __init__(self):
        self.operands = []

    def App(self, graph):
        return self.visit(graph.operator)

    def Fun(self, graph):
        return self.visit(graph.children)

    def Op(self, graph):
        opname = graph.__class__.__name__

        annot = annotation(graph)
        children = self.visit(graph.children)

        # TODO: Set classifier directly on graph nodes?
        if graph.is_arithmetic:
            classifier = 'Arithmetic'
        elif graph.is_math:
            classifier = 'Math'
        elif graph.is_reduction:
            classifier = 'Reduction'
        else:
            classifier = None

        if classifier:
            return AAppl(ATerm(classifier),
                         [ATerm(opname)] + children,
                         annotation=annot)
        else:
            return AAppl(ATerm(opname), children,
                         annotation=annot)

    def Literal(self, graph):
        if graph.vtype == int:
            return AInt(graph.val, annotation=annotation(graph))
        if graph.vtype == float:
            return AFloat(graph.val, annotation=annotation(graph))
        else:
            return ATerm(graph.val, annotation=annotation(graph))

    def Indexable(self, graph):
        self.operands.append(graph)
        return AAppl(ATerm('Array'), [], annotation=annotation(graph))

    def Slice(self, graph):
        # Slice(start, stop, step){id(graph), 'get'|'set'}
        array, start, stop, step = graph.operands

        if start:
            start = self.visit(start)
        if stop:
            stop = self.visit(stop)
        if step:
            step = self.visit(step)

        return AAppl(
            ATerm('Slice'),
            [self.visit(array),
             start or ATerm('None'),
             stop  or ATerm('None'),
             step  or ATerm('None')],
            annotation=annotation(graph, graph.op)
        )

    def IndexNode(self, graph):
        return AAppl(ATerm('Index'), self.visit(graph.operands),
                     annotation=annotation(graph, graph.op))

    def Assign(self, graph):
        return AAppl(ATerm('Assign'), self.visit(graph.operands),
                     annotation=annotation(graph))
