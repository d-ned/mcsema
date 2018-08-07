# Copyright (c) 2018 Trail of Bits, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pprint
import collections
import binaryninja as binja
from binja_var_recovery.util import *

FUNCTION_OBJECTS = collections.defaultdict()

class VariableAliasSet(object):
  def __init__(self):
    self.ALIAS_SET = collections.defaultdict(int)
    
  def add(self, start_addr, alias_addr):
    try:
      value = self.ALIAS_SET[start_addr]
      if value < alias_addr:
        self.ALIAS_SET[start_addr] =  alias_addr
    except KeyError:
      index = None
      for k, v in self.ALIAS_SET:
        if start_addr > k:
          break
        index = k
    
      if index:
        value = self.ALIAS_SET[index]
        if value < alias_addr:
          self.ALIAS_SET[start_addr] =  alias_addr

  def __repr__(self):
    string = "{ "
    for k in sorted(self.ALIAS_SET.keys()):
      string += "({:x} : {:x})".format(k, self.ALIAS_SET[k])
    string += " }"
    return string
    
VARIABLE_ALIAS_SET = VariableAliasSet()
SSA_VARIABLE_VALUESET = collections.defaultdict(dict)

class PossibleValueSet(object):
  def __init__(self, value):
    self.type = type
    self.value = value

class ILVisitor(object):
  """ Class functions to visit medium-level IL"""
  def __init__(self):
    super(ILVisitor, self).__init__()

  def visit(self, expr):
    method_name = 'visit_{}'.format(expr.operation.name)
    if hasattr(self, method_name):
      value = getattr(self, method_name)(expr)
    else:
      DEBUG("Warning! method `{}` not found.".format(method_name))
      value = None
    return value

class SSAVariable(ILVisitor):
  def __init__(self, var, address_size, func=None):
    super(SSAVariable, self).__init__()
    self.address_size = address_size
    self.var = var
    self.function = func.medium_level_il.ssa_form
    self.var_name = "{}#{}".format(var.var.name, var.version)
    self.func_start = func.start
    self.visited = set()
    self.to_visit = list()
    self.value_set = set()

  def get_values(self):
    var_def = self.function.get_ssa_var_definition(self.var)
    self.to_visit.append(var_def)
    while self.to_visit:
      idx = self.to_visit.pop()
      if idx is not None:
        DEBUG("visit {}".format(self.function[idx]))
        self.visit(self.function[idx])

    SSA_VARIABLE_VALUESET[self.func_start][self.var_name] = set()
    for item in self.value_set:
      SSA_VARIABLE_VALUESET[self.func_start][self.var_name].add(item)
    return self.value_set

  def visit_MLIL_CONST(self, expr):
    values = set()
    values.add("<const {:x}>".format(expr.constant))
    return values

  def visit_MLIL_CONST_PTR(self, expr):
    values = set()
    values.add("<const ptr {:x}>".format(expr.constant))
    return values

  def visit_MLIL_SET_VAR_ALIASED(self, expr):
    DEBUG("visit_MLIL_SET_VAR_ALIASED: {}".format(expr))
    src = self.visit(expr.src)
    return src

  def visit_MLIL_VAR_ALIASED(self, expr):
    DEBUG("visit_MLIL_VAR_ALIASED: {}".format(expr))
    values = set()
    values.add(expr.src)
    return values

  def visit_MLIL_SET_VAR_SSA(self, expr):
    ssa_var = expr.dest
    if isinstance(ssa_var, binja.SSAVariable):
      possible_value = expr.get_ssa_var_possible_values(ssa_var)
      if possible_value.type == binja.RegisterValueType.EntryValue:
        reg_name = possible_value.reg
        try:
          func_obj = FUNCTION_OBJECTS[self.func_start]
          values = func_obj.get_entry_register(reg_name)
          for item in values:
            self.value_set.add(item)
        except:
          self.value_set.add(possible_value)
      elif possible_value.type != binja.RegisterValueType.UndeterminedValue:
        self.value_set.add(possible_value)
        DEBUG("visit_MLIL_SET_VAR_SSA: possible values {}".format(possible_value))
      else:
        src = self.visit(expr.src)
    else:
      DEBUG("visit_MLIL_SET_VAR_SSA: Warning! The dest is not ssa variable")

    return self.value_set

  def visit_MLIL_VAR_SSA(self, expr):
    """ Get the possible value of the ssa variable
    """
    value_set = set()
    ssa_var = expr.src
    if isinstance(ssa_var, binja.SSAVariable):
      possible_value = expr.get_ssa_var_possible_values(ssa_var)
      
      if possible_value.type != binja.RegisterValueType.UndeterminedValue:
        self.value_set.add(possible_value)
        return self.value_set
      else:
        if ssa_var.var.name == "__return_addr":
          self.value_set.add(ssa_var)
          return self.value_set

        elif expr.src not in self.visited:
          var_def = expr.function.get_ssa_var_definition(expr.src)
          if var_def is not None:
            self.to_visit.append(var_def)

    return self.value_set

  def visit_MLIL_LOAD_SSA(self, expr):
    """ Resolve the SSA Variable performing memory load
    """
    isrc = expr.src
    if isrc.operation == binja.MediumLevelILOperation.MLIL_CONST_PTR or \
      isrc.operation == binja.MediumLevelILOperation.MLIL_CONST:
      memory = isrc.constant
    else:
      memory = self.visit(isrc)
      DEBUG("visit_MLIL_LOAD_SSA {} {} operations {}".format(expr.src, memory, isrc.operation))

  def visit_MLIL_ADD(self, expr):
    """ Resolve the SSA variable used in the addition expression
    """
    values = set()
    left = self.visit(expr.left)
    if left:
      for item in left:
        values.add(item)
      
    right = self.visit(expr.right)
    if right:
      for item in right:
        values.add(item)
    DEBUG("visit_MLIL_ADD values {} ".format(values))
    return values

  def visit_MLIL_VAR_PHI(self, expr):
    """ Resolve the MLIL_VAR_PHI operation
        Handling VAR_PHI causing the circular dependency; Disable it if the variable value is undef
    """
    DEBUG("visit_MLIL_VAR_PHI expr {} ".format(expr.src))
    for ssa_var in expr.src:
      possible_value = expr.get_ssa_var_possible_values(ssa_var)

      if possible_value.type != binja.RegisterValueType.UndeterminedValue:
        self.value_set.add(possible_value)
        return self.value_set
      else:
        self.value_set.add(ssa_var)
        #var_def = expr.function.get_ssa_var_definition(ssa_var)
        #if var_def is not None:
        #self.to_visit.append(var_def)
