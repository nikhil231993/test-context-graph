"""Java source -> JavaFile, with tree-sitter-java. Pure: no file, network or clock access.

What is extracted is documented on the dataclasses in models.py. Choices made here:

- Nested types are flattened into JavaFile.types with dotted names ("Outer.Inner"). Anonymous and
  local classes are not types: their calls, like calls inside lambdas, belong to the enclosing
  method. Calls in field initializers, initializer blocks and enum constant bodies belong to no
  method and are not recorded.
- CallSite.receiver_type comes from the same file only. Inside a method, scoping is lexical: a
  variable is visible from its declaration to the end of the node that declares it (block, for,
  catch, lambda, try-with-resources, switch case); a pattern variable (`o instanceof Owner x`) to
  the end of the method. Then the fields of the enclosing type and its outer types (record
  components and enum constants count as fields), then a capitalised identifier is taken as a type
  name (a static call). A type variable becomes its first bound ("T extends BaseEntity" ->
  "BaseEntity") or None. Chained calls and other computed receivers give None; so do `var`
  variables unless initialised with `new`, a cast or a literal, and multi-catch parameters.
- Source text kept in the facts (receivers, annotation values, initializers, signatures) is put on
  one line: comments dropped, whitespace runs collapsed to one space, and line breaks next to
  '.', '(' or ')' removed, so a multi-line call chain reads "a.b(x).c()".
- A constructor reference `Owner::new` is a "method_ref" named "<init>"; `this(...)` and
  `super(...)` in constructors are "call"s named "<init>" with receiver "this" or "super".
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from typing import Any

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from .models import (
    Annotation,
    CallSite,
    FieldDecl,
    Import,
    JavaFile,
    MethodDecl,
    Parameter,
    TypeDecl,
    TypeRef,
)

MAX_ARG_TEXT = 200  # CallSite.args: a long lambda argument is cut here
_LANGUAGE = Language(tree_sitter_java.language())
_PARSER = Parser(_LANGUAGE)

_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_METHOD_NODES = {
    "method_declaration",
    "constructor_declaration",
    "compact_constructor_declaration",
    "annotation_type_element_declaration",
}
_CONSTRUCTOR_NODES = {"constructor_declaration", "compact_constructor_declaration"}
_FIELD_NODES = {"field_declaration", "constant_declaration"}
_ANNOTATION_NODES = {"annotation", "marker_annotation"}
_COMMENT_NODES = {"line_comment", "block_comment"}
_TYPE_NODES = {
    "type_identifier",
    "scoped_type_identifier",
    "generic_type",
    "array_type",
    "integral_type",
    "floating_point_type",
    "boolean_type",
    "void_type",
    "annotated_type",
    "wildcard",
}
_CALL_NODES = {
    "method_invocation",
    "object_creation_expression",
    "method_reference",
    "explicit_constructor_invocation",
}
_MAX_ERRORS = 5

# FieldDecl has no javadoc in the contract yet; fill it as soon as it gets one.
_FIELD_HAS_JAVADOC = "javadoc" in {f.name for f in fields(FieldDecl)}


def parse_java(source: str | bytes, path: str) -> JavaFile:
    """Parse one .java file. Never raises on bad Java: syntax problems go to JavaFile.errors."""
    data = source.encode("utf-8") if isinstance(source, str) else source
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    root = _PARSER.parse(data).root_node
    result = JavaFile(path=path, package="")
    for node in _top_level(root):
        if node.type == "package_declaration":
            result.package = _package_name(node)
        elif node.type == "import_declaration":
            result.imports.append(_import(node))
        else:
            syntax = _type_syntax(node)
            if syntax is not None:
                _type_decl(syntax, result.package, None, result.types)
    if root.has_error:
        result.errors = _syntax_errors(root, data)
    return result


# ---- file level --------------------------------------------------------------------------


def _top_level(root: Node) -> Iterator[Node]:
    """The root's declarations. From an ERROR node (tree-sitter's recovery from a syntax error),
    the package and imports in it, and the ERROR itself when a type can be salvaged from it."""
    stack = list(reversed(root.named_children))
    while stack:
        node = stack.pop()
        if node.type != "ERROR":
            yield node
            continue
        keyword = _type_keyword(node)
        if keyword is None:
            stack.extend(reversed(node.named_children))
            continue
        for child in node.children[:keyword]:
            if child.type in ("package_declaration", "import_declaration"):
                yield child
        yield node


def _package_name(node: Node) -> str:
    name = _first_child(node, {"scoped_identifier", "identifier"})
    return _squash(name) if name else ""


def _import(node: Node) -> Import:
    name = _first_child(node, {"scoped_identifier", "identifier"})
    kinds = {child.type for child in node.children}
    return Import(
        name=_squash(name) if name else "", static="static" in kinds, wildcard="asterisk" in kinds
    )


def _syntax_errors(root: Node, data: bytes) -> list[str]:
    """The first ERROR/MISSING nodes, as "line N: syntax error near '...'". An ERROR node that
    contains others is not reported itself: the inner ones are closer to the mistake."""
    found: list[Node] = []
    stack = [root]
    while stack and len(found) < 50:
        node = stack.pop()
        if node.is_missing or node.is_error:
            found.append(node)
        if node.has_error and not node.is_missing:
            stack.extend(reversed(node.children))
    innermost = [
        node
        for node in found
        if not any(
            other is not node
            and node.start_byte <= other.start_byte
            and other.end_byte <= node.end_byte
            and (other.start_byte, other.end_byte) != (node.start_byte, node.end_byte)
            for other in found
        )
    ]
    lines = data.split(b"\n")  # as tree-sitter counts rows
    errors = []
    for node in innermost[:_MAX_ERRORS]:
        if node.is_missing:  # zero width: show the line it is missing from
            row = node.start_point[0]
            near = _shorten(lines[row].decode("utf-8", "replace").strip() if row < len(lines) else "")
            errors.append(f"line {_line(node)}: syntax error near '{near}' (missing '{node.type}')")
        else:
            near = _shorten(_one_line(_text(node)))
            errors.append(f"line {_line(node)}: syntax error near '{near}'")
    return errors


def _shorten(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---- types -------------------------------------------------------------------------------


@dataclass
class _TypeSyntax:
    """The parts of a type declaration: a well-formed one, or one salvaged from an ERROR node."""

    node: Node  # the declaration, or the ERROR node: for lines and javadoc
    kind: str
    name: str
    modifiers: Node | None
    type_params: Node | None
    superclass: Node | None
    interfaces: Node | None  # super_interfaces: implements ...
    extends_interfaces: Node | None  # an interface's extends ...
    record_params: Node | None
    members: list[Node]


_TYPE_KEYWORDS = {
    "class": "class",
    "interface": "interface",
    "enum": "enum",
    "record": "record",
    "@interface": "annotation",
}
_BODY_NODES = {"class_body", "interface_body", "enum_body", "annotation_type_body"}


def _type_syntax(node: Node) -> _TypeSyntax | None:
    if node.type == "ERROR":
        return _salvaged_type_syntax(node)
    name = node.child_by_field_name("name")
    if node.type not in _TYPE_KINDS or name is None:
        return None
    return _TypeSyntax(
        node=node,
        kind=_TYPE_KINDS[node.type],
        name=_text(name),
        modifiers=_first_child(node, {"modifiers"}),
        type_params=node.child_by_field_name("type_parameters"),
        superclass=node.child_by_field_name("superclass"),
        interfaces=node.child_by_field_name("interfaces"),
        extends_interfaces=_first_child(node, {"extends_interfaces"}),
        record_params=(
            node.child_by_field_name("parameters") if node.type == "record_declaration" else None
        ),
        members=list(_members(node.child_by_field_name("body"))),
    )


def _type_keyword(node: Node) -> int | None:
    """The index of a 'class'/'interface'/... keyword among an ERROR node's children."""
    children = node.children
    for index, child in enumerate(children[:-1]):
        if not child.is_named and child.type in _TYPE_KEYWORDS:
            if children[index + 1].type == "identifier":
                return index
    return None


def _salvaged_type_syntax(error: Node) -> _TypeSyntax | None:
    """A type whose declaration tree-sitter broke up into an ERROR node after a syntax error
    further down: keep what can be read (its header and the members that parsed)."""
    index = _type_keyword(error)
    if index is None:
        return None
    children = error.children
    keyword, name = children[index], children[index + 1]
    before = children[index - 1] if index > 0 else None
    after = children[index + 2 :]
    members: list[Node] = []
    for child in after:
        if child.type in _BODY_NODES or (child.type == "ERROR" and _type_keyword(child) is None):
            members.extend(_members(child))
        elif child.is_named and child.type not in _COMMENT_NODES:
            members.append(child)
    return _TypeSyntax(
        node=error,
        kind=_TYPE_KEYWORDS[keyword.type],
        name=_text(name),
        modifiers=before if before is not None and before.type == "modifiers" else None,
        type_params=_first_child_of(after, "type_parameters"),
        superclass=_first_child_of(after, "superclass"),
        interfaces=_first_child_of(after, "super_interfaces"),
        extends_interfaces=_first_child_of(after, "extends_interfaces"),
        record_params=(
            _first_child_of(after, "formal_parameters") if keyword.type == "record" else None
        ),
        members=members,
    )


def _first_child_of(nodes: list[Node], node_type: str) -> Node | None:
    return next((node for node in nodes if node.type == node_type), None)


@dataclass
class _TypeCtx:
    """What a method body can see of its enclosing types: field types and type variables."""

    decl: TypeDecl
    superclass: str | None
    type_params: dict[str, str | None]  # type variable -> its first bound's name, or None
    fields: dict[str, str | None] = field(default_factory=dict)  # field -> type name
    outer: _TypeCtx | None = None

    def erase(self, name: str) -> str | None:
        ctx: _TypeCtx | None = self
        while ctx is not None:
            if name in ctx.type_params:
                return ctx.type_params[name]
            ctx = ctx.outer
        return name

    def field_type(self, name: str) -> tuple[bool, str | None]:
        ctx: _TypeCtx | None = self
        while ctx is not None:
            if name in ctx.fields:
                return True, ctx.fields[name]
            ctx = ctx.outer
        return False, None


def _type_decl(
    syntax: _TypeSyntax, package: str, outer: _TypeCtx | None, out: list[TypeDecl]
) -> None:
    """Append the type and its nested types to `out`, outer first (source order)."""
    name = f"{outer.decl.name}.{syntax.name}" if outer else syntax.name
    modifiers, annotations = _modifiers_of(syntax.modifiers)
    if syntax.kind == "class":
        extends = [_type_ref(t) for t in _named(syntax.superclass) if t.type in _TYPE_NODES]
    else:
        extends = _type_list(syntax.extends_interfaces)
    decl = TypeDecl(
        name=name,
        qualified_name=f"{package}.{name}" if package else name,
        kind=syntax.kind,
        start_line=_line(syntax.node),
        end_line=_end_line(syntax.node),
        modifiers=modifiers,
        annotations=annotations,
        type_parameters=[
            _one_line(_text(p)) for p in _named(syntax.type_params) if p.type == "type_parameter"
        ],
        extends=extends,
        implements=_type_list(syntax.interfaces),
        record_components=_parameters(syntax.record_params),
        javadoc=_javadoc(syntax.node),
        outer=outer.decl.qualified_name if outer else None,
    )
    out.append(decl)

    for member in syntax.members:
        if member.type in _FIELD_NODES:
            decl.fields.extend(_fields(member))
        elif member.type == "enum_constant":
            constant = member.child_by_field_name("name")
            if constant is not None:
                decl.enum_constants.append(_text(constant))
    ctx = _type_ctx(decl, syntax.type_params, outer)
    for member in syntax.members:
        if member.type in _METHOD_NODES:
            method = _method(member, ctx)
            if method is not None:
                decl.methods.append(method)
        else:
            nested = _type_syntax(member)
            if nested is not None:
                _type_decl(nested, package, ctx, out)


def _type_ctx(decl: TypeDecl, type_params: Node | None, outer: _TypeCtx | None) -> _TypeCtx:
    superclass = decl.extends[0].name if decl.kind == "class" and decl.extends else None
    ctx = _TypeCtx(decl, superclass, _bounds(type_params), outer=outer)
    for f in decl.fields:
        ctx.fields[f.name] = ctx.erase(f.type.name)
    for component in decl.record_components:
        ctx.fields.setdefault(component.name, ctx.erase(component.type.name))
    for constant in decl.enum_constants:
        ctx.fields.setdefault(constant, decl.name)
    return ctx


def _members(body: Node | None) -> Iterator[Node]:
    """A type body's declarations. Those tree-sitter put in an ERROR node (after a syntax error)
    are taken out of it, unless a nested type can be salvaged from that ERROR."""
    for child in _named(body):
        if child.type == "enum_body_declarations" or (
            child.type == "ERROR" and _type_keyword(child) is None
        ):
            yield from _members(child)
        else:
            yield child


def _type_list(node: Node | None) -> list[TypeRef]:
    """The types of a super_interfaces / extends_interfaces clause."""
    types = _first_child(node, {"type_list"}) if node is not None else None
    return [_type_ref(t) for t in _named(types) if t.type in _TYPE_NODES]


def _bounds(type_params: Node | None) -> dict[str, str | None]:
    """Type variable -> the name of its first bound, which is what it erases to (None: Object)."""
    result: dict[str, str | None] = {}
    for param in _named(type_params):
        if param.type != "type_parameter":
            continue
        name = _first_child(param, {"type_identifier", "identifier"})
        bound = _first_child(param, {"type_bound"})
        first = _first_child(bound, _TYPE_NODES) if bound is not None else None
        if name is not None:
            result[_text(name)] = _type_ref(first).name if first is not None else None
    return result


def _fields(node: Node) -> list[FieldDecl]:
    """One FieldDecl per declared name: `int a, b;` gives two."""
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return []
    modifiers, annotations = _modifiers(node)
    extra: dict[str, Any] = {"javadoc": _javadoc(node)} if _FIELD_HAS_JAVADOC else {}
    result = []
    for declarator in node.children_by_field_name("declarator"):
        name = declarator.child_by_field_name("name")
        if name is None:
            continue
        value = declarator.child_by_field_name("value")
        result.append(
            FieldDecl(
                name=_text(name),
                type=_type_ref(type_node, extra_dims=_dims(declarator)),
                line=_line(name),
                modifiers=list(modifiers),
                annotations=list(annotations),
                initializer=_one_line(_text(value)) if value is not None else None,
                **extra,
            )
        )
    return result


# ---- methods -----------------------------------------------------------------------------


def _method(node: Node, ctx: _TypeCtx) -> MethodDecl | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    is_constructor = node.type in _CONSTRUCTOR_NODES
    modifiers, annotations = _modifiers(node)
    if node.type == "compact_constructor_declaration":
        # The canonical constructor: its parameters are the record components.
        params = [
            Parameter(c.name, c.type, list(c.annotations)) for c in ctx.decl.record_components
        ]
    else:
        params = _parameters(node.child_by_field_name("parameters"))
    return_type = None
    if not is_constructor:
        type_node = node.child_by_field_name("type")
        return_type = _type_ref(type_node, extra_dims=_dims(node)) if type_node else None
    throws_node = _first_child(node, {"throws"})
    throws = [_one_line(_text(t)) for t in _named(throws_node) if t.type in _TYPE_NODES]
    type_params = node.child_by_field_name("type_parameters")
    body = node.child_by_field_name("body")
    calls = _MethodScope(node, ctx, params, type_params).calls(body) if body is not None else []
    return MethodDecl(
        name="<init>" if is_constructor else _text(name_node),
        kind="constructor" if is_constructor else "method",
        start_line=_line(node),
        end_line=_end_line(node),
        signature=_signature(
            modifiers, type_params, return_type, _text(name_node), params, throws
        ),
        parameters=params,
        return_type=return_type,
        modifiers=modifiers,
        annotations=annotations,
        throws=throws,
        calls=calls,
        javadoc=_javadoc(node),
        has_body=body is not None,
    )


def _parameters(node: Node | None) -> list[Parameter]:
    return [p for p in map(_parameter, _named(node)) if p is not None]


def _parameter(node: Node) -> Parameter | None:
    """A formal_parameter, or a spread_parameter (`String... names`); None for anything else."""
    if node.type == "formal_parameter":
        name = node.child_by_field_name("name")
        type_node = node.child_by_field_name("type")
        if name is None or type_node is None:
            return None
        type_ref = _type_ref(type_node, extra_dims=_dims(node))
    elif node.type == "spread_parameter":
        type_node = _first_child(node, _TYPE_NODES)
        declarator = _first_child(node, {"variable_declarator"})
        name = declarator.child_by_field_name("name") if declarator is not None else None
        if name is None or type_node is None:
            return None
        type_ref = _type_ref(type_node, varargs=True)
    else:
        return None
    return Parameter(name=_text(name), type=type_ref, annotations=_modifiers(node)[1])


def _signature(
    modifiers: list[str],
    type_params: Node | None,
    return_type: TypeRef | None,
    name: str,
    params: list[Parameter],
    throws: list[str],
) -> str:
    """'public static <T> T getById(Collection<T> items, int id) throws X': no annotations."""
    head = list(modifiers)
    if type_params is not None:
        head.append(_one_line(_text(type_params)))
    if return_type is not None:
        head.append(return_type.text)
    head.append(f"{name}({', '.join(f'{p.type.text} {p.name}' for p in params)})")
    signature = " ".join(head)
    if throws:
        signature += " throws " + ", ".join(throws)
    return signature


# ---- call sites --------------------------------------------------------------------------


@dataclass
class _Var:
    """A local variable: visible from `pos` on, inside its scope [start, end) (bytes)."""

    type: str | None
    pos: int
    start: int
    end: int


class _MethodScope:
    """Finds a method's call sites and the local variables visible at each, to tell receiver
    types. See the module docstring for the scoping rules."""

    def __init__(
        self, method: Node, ctx: _TypeCtx, params: list[Parameter], type_params: Node | None
    ) -> None:
        self.method = method
        self.ctx = ctx
        self.type_params = _bounds(type_params)
        self.vars: dict[str, list[_Var]] = {}
        self.inferred: list[tuple[_Var, Node | None]] = []  # `var` variables and initialisers
        for param in params:
            self._declare(param.name, self._erase(param.type.name), method.start_byte, method)

    def calls(self, body: Node) -> list[CallSite]:
        found: list[Node] = []
        stack = [body]
        while stack:
            node = stack.pop()
            if node.type in _CALL_NODES:
                found.append(node)
            else:
                self._declarations(node)
            stack.extend(reversed(node.named_children))
        for var, initializer in self.inferred:
            var.type = self.expr_type(initializer, type_name=False) if initializer else None
        sites = [site for site in map(self._call_site, found) if site is not None]
        return [site for _, site in sorted(sites, key=lambda pair: pair[0])]

    # -- variables --

    def _declare(self, name: str, type_name: str | None, pos: int, scope: Node) -> _Var:
        var = _Var(type=type_name, pos=pos, start=scope.start_byte, end=scope.end_byte)
        self.vars.setdefault(name, []).append(var)
        return var

    def _declare_typed(
        self,
        name: Node | None,
        type_node: Node | None,
        scope: Node,
        initializer: Node | None = None,
        pos: int | None = None,
    ) -> None:
        """Declare a variable with a written type; `var` is inferred from the initializer."""
        if name is None:
            return
        start = name.start_byte if pos is None else pos
        if type_node is None or _text(type_node) == "var":
            self.inferred.append((self._declare(_text(name), None, start, scope), initializer))
        else:
            self._declare(_text(name), self._erase(_type_ref(type_node).name), start, scope)

    def _declarations(self, node: Node) -> None:
        kind = node.type
        if kind in ("local_variable_declaration", "field_declaration"):
            # Fields here belong to an anonymous or local class: visible in all its body.
            scope = node.parent or self.method
            pos = scope.start_byte if kind == "field_declaration" else None
            type_node = node.child_by_field_name("type")
            for declarator in node.children_by_field_name("declarator"):
                self._declare_typed(
                    declarator.child_by_field_name("name"),
                    type_node,
                    scope,
                    declarator.child_by_field_name("value"),
                    pos,
                )
        elif kind in ("formal_parameter", "spread_parameter"):
            # A lambda's, or a method's of an anonymous or local class: visible in all of it.
            param = _parameter(node)
            owner = node.parent.parent if node.parent is not None else None
            if param is not None and owner is not None:
                type_name = None if param.type.name == "var" else self._erase(param.type.name)
                self._declare(param.name, type_name, owner.start_byte, owner)
        elif kind == "lambda_expression":
            params = node.child_by_field_name("parameters")
            if params is not None and params.type == "identifier":
                self._declare(_text(params), None, params.start_byte, node)
            elif params is not None and params.type == "inferred_parameters":
                for name in _named(params):
                    self._declare(_text(name), None, name.start_byte, node)
        elif kind == "enhanced_for_statement":
            self._declare_typed(
                node.child_by_field_name("name"), node.child_by_field_name("type"), node
            )
        elif kind == "catch_formal_parameter":
            catch_type = _first_child(node, {"catch_type"})
            types = [t for t in _named(catch_type) if t.type in _TYPE_NODES]
            name = node.child_by_field_name("name")
            if name is not None:
                # A multi-catch variable's type is a union: unknown.
                type_name = _type_ref(types[0]).name if len(types) == 1 else None
                self._declare(_text(name), type_name, name.start_byte, node.parent or node)
        elif kind == "resource":
            statement = node.parent.parent if node.parent is not None else None
            self._declare_typed(
                node.child_by_field_name("name"),
                node.child_by_field_name("type"),
                statement or self.method,
                node.child_by_field_name("value"),
            )
        elif kind == "instanceof_expression":
            self._declare_typed(
                node.child_by_field_name("name"), node.child_by_field_name("right"), self.method
            )
        elif kind == "type_pattern":
            type_node = _first_child(node, _TYPE_NODES)
            names = [c for c in _named(node) if c.type == "identifier"]
            scope = _ancestor(node, {"switch_rule", "switch_block_statement_group"})
            if names:
                self._declare_typed(names[-1], type_node, scope or self.method)

    def _lookup(self, name: str, pos: int) -> tuple[bool, str | None]:
        """The innermost, latest declaration of `name` visible at byte `pos`."""
        best: _Var | None = None
        for var in self.vars.get(name, ()):
            if var.start <= pos < var.end and var.pos <= pos:
                if best is None or var.pos >= best.pos:
                    best = var
        return (True, best.type) if best is not None else (False, None)

    def _is_variable(self, name: str, pos: int) -> bool:
        return self._lookup(name, pos)[0] or self.ctx.field_type(name)[0]

    def _erase(self, type_name: str) -> str | None:
        if type_name in self.type_params:
            return self.type_params[type_name]
        return self.ctx.erase(type_name)

    # -- receivers --

    def expr_type(self, node: Node, type_name: bool = True) -> str | None:
        """The type name of an expression when this file tells it, else None. With `type_name`,
        a bare (qualified) type name counts too, as the receiver of a static call."""
        kind = node.type
        if kind == "identifier":
            name = _text(node)
            found, found_type = self._lookup(name, node.start_byte)
            if not found:
                found, found_type = self.ctx.field_type(name)
            if found:
                return found_type
            return name if type_name and _looks_like_type(name) else None
        if kind == "this":
            return self.ctx.decl.name
        if kind == "super":
            return self.ctx.superclass
        if kind == "field_access":
            obj = node.child_by_field_name("object")
            name = node.child_by_field_name("field")
            if obj is None or name is None:
                return None
            if name.type == "this":  # Outer.this
                outer = self._qualified_this(obj)
                return outer.decl.name if outer is not None else None
            if obj.type == "this":
                return self.ctx.fields.get(_text(name))
            obj_field = obj.child_by_field_name("field") if obj.type == "field_access" else None
            if obj_field is not None and obj_field.type == "this":  # Outer.this.field
                outer = self._qualified_this(obj.child_by_field_name("object"))
                return outer.fields.get(_text(name)) if outer is not None else None
            return self._qualified_type(node) if type_name else None
        if kind == "array_access":  # TypeRef.name has no [], so an array's name is its element's
            array = node.child_by_field_name("array")
            return self.expr_type(array, type_name=False) if array is not None else None
        if kind == "parenthesized_expression":
            inner = _named(node)
            return self.expr_type(inner[0], type_name=False) if len(inner) == 1 else None
        if kind in ("cast_expression", "object_creation_expression"):
            type_node = node.child_by_field_name("type")
            return _type_ref(type_node).name if type_node is not None else None
        if kind == "string_literal":
            return "String"
        if kind == "class_literal":
            return "Class"
        if kind in _TYPE_NODES:  # the left side of Type::method
            return self._erase(_type_ref(node).name)
        return None

    def _qualified_this(self, qualifier: Node | None) -> _TypeCtx | None:
        """The enclosing type named by the `Outer` of `Outer.this`."""
        if qualifier is None:
            return None
        name = _squash(qualifier)
        ctx: _TypeCtx | None = self.ctx
        while ctx is not None:
            if ctx.decl.name == name or ctx.decl.name.endswith("." + name):
                return ctx
            ctx = ctx.outer
        return None

    def _qualified_type(self, node: Node) -> str | None:
        """'java.util.Objects' or 'Outer.Inner' as a receiver: a type name, unless the first part
        is a variable. 'HttpStatus.OK' is a constant, not a type."""
        parts = _identifier_chain(node)
        if not parts or self._is_variable(parts[0], node.start_byte):
            return None
        first_type = next((i for i, part in enumerate(parts) if part[:1].isupper()), None)
        if first_type is None or not _looks_like_type(parts[first_type]):
            return None
        if any(not part[:1].islower() for part in parts[:first_type]):
            return None
        nested = parts[first_type + 1 :]
        if any(not (_looks_like_type(p) and any(ch.islower() for ch in p)) for p in nested):
            return None
        return ".".join(parts)

    def _call_site(self, node: Node) -> tuple[int, CallSite] | None:
        """(sort key, CallSite): calls are ordered by the position of their name."""
        kind = node.type
        if kind == "method_invocation":
            name = node.child_by_field_name("name")
            if name is None:
                return None
            receiver, receiver_type = self._receiver(node)
            site = CallSite(
                kind="call",
                name=_text(name),
                line=_line(name),
                receiver=receiver,
                receiver_type=receiver_type,
                **_arguments(node.child_by_field_name("arguments")),
            )
            return name.start_byte, site
        if kind == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is None:
                return None
            type_name = _type_ref(type_node).name
            site = CallSite(
                kind="new",
                name=type_name,
                line=_line(type_node),
                receiver_type=type_name,
                **_arguments(node.child_by_field_name("arguments")),
            )
            return type_node.start_byte, site
        if kind == "method_reference":
            parts = _named(node)
            last = node.children[-1] if node.children else None
            if not parts or last is None or last == parts[0]:
                return None
            site = CallSite(
                kind="method_ref",
                name="<init>" if last.type == "new" else _text(last),
                line=_line(last),
                receiver=_one_line(_text(parts[0])),
                receiver_type=self.expr_type(parts[0]),
            )
            return last.start_byte, site
        # explicit_constructor_invocation: this(...) or super(...)
        keyword = node.child_by_field_name("constructor")
        if keyword is None:
            return None
        is_this = keyword.type == "this"
        site = CallSite(
            kind="call",
            name="<init>",
            line=_line(keyword),
            receiver=_text(keyword),
            receiver_type=self.ctx.decl.name if is_this else self.ctx.superclass,
            **_arguments(node.child_by_field_name("arguments")),
        )
        return keyword.start_byte, site

    def _receiver(self, call: Node) -> tuple[str | None, str | None]:
        obj = call.child_by_field_name("object")
        if obj is None:
            return None, None
        explicit_super = _first_child(call, {"super"})
        if explicit_super is not None:  # Iface.super.method(): Iface's default method
            text = (call.text or b"")[
                obj.start_byte - call.start_byte : explicit_super.end_byte - call.start_byte
            ]
            return _one_line(text.decode("utf-8", "replace")), self.expr_type(obj)
        return _one_line(_text(obj)), self.expr_type(obj)


def _identifier_chain(node: Node) -> list[str]:
    """['java', 'util', 'Objects'] for java.util.Objects; [] if any part isn't an identifier."""
    parts: list[str] = []
    while node.type == "field_access":
        name = node.child_by_field_name("field")
        obj = node.child_by_field_name("object")
        if name is None or obj is None or name.type != "identifier":
            return []
        parts.append(_text(name))
        node = obj
    if node.type != "identifier":
        return []
    parts.append(_text(node))
    return parts[::-1]


def _looks_like_type(name: str) -> bool:
    """Capitalised, and not a CONSTANT_NAME."""
    return name[:1].isupper() and not ("_" in name and name.upper() == name)


def _arguments(args: Node | None) -> dict[str, Any]:
    values = _named(args)
    strings = [s for s in map(_string_arg, values) if s is not None]
    texts = [_one_line(_text(value))[:MAX_ARG_TEXT] for value in values]
    return {"arg_count": len(values), "string_args": strings, "args": texts}


def _string_arg(node: Node) -> str | None:
    """A string literal unquoted; a '+' concatenation with each non-literal part as '{}'."""
    if node.type == "string_literal":
        return _string_value(node)
    if node.type != "binary_expression" or _operator(node) != "+":
        return None
    parts = _concat_parts(node)
    if not any(part.type == "string_literal" for part in parts):
        return None
    return "".join(_string_value(p) if p.type == "string_literal" else "{}" for p in parts)


def _concat_parts(node: Node) -> list[Node]:
    """The operands of a chain of '+', left to right. Iterative: chains can be long."""
    parts: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "binary_expression" and _operator(current) == "+":
            left = current.child_by_field_name("left")
            right = current.child_by_field_name("right")
            stack.extend(n for n in (right, left) if n is not None)
        else:
            parts.append(current)
    return parts


def _operator(node: Node) -> str | None:
    operator = node.child_by_field_name("operator")
    return operator.type if operator is not None else None


# ---- annotations, modifiers, javadoc -----------------------------------------------------


def _modifiers(node: Node) -> tuple[list[str], list[Annotation]]:
    """Keywords ('public', 'static'...) and annotations, from the node's modifiers."""
    return _modifiers_of(_first_child(node, {"modifiers"}))


def _modifiers_of(modifiers: Node | None) -> tuple[list[str], list[Annotation]]:
    if modifiers is None:
        return [], []
    keywords = [_text(child) for child in modifiers.children if not child.is_named]
    annotations = [_annotation(c) for c in modifiers.children if c.type in _ANNOTATION_NODES]
    return keywords, annotations


def _annotation(node: Node) -> Annotation:
    name = node.child_by_field_name("name")
    arguments: dict[str, Any] = {}
    for item in _named(node.child_by_field_name("arguments")):
        if item.type == "element_value_pair":
            key = item.child_by_field_name("key")
            value = item.child_by_field_name("value")
            if key is not None and value is not None:
                arguments[_text(key)] = _annotation_value(value)
        else:
            arguments["value"] = _annotation_value(item)
    return Annotation(name=_squash(name).rsplit(".", 1)[-1] if name else "", arguments=arguments)


def _annotation_value(node: Node) -> Any:
    """A string unquoted, an array as a list (each element the same way), else the raw text."""
    if node.type == "string_literal":
        return _string_value(node)
    if node.type == "element_value_array_initializer":
        return [_annotation_value(item) for item in _named(node)]
    if node.type == "binary_expression" and _operator(node) == "+":
        parts = _concat_parts(node)
        if all(part.type == "string_literal" for part in parts):
            return "".join(_string_value(part) for part in parts)
    return _one_line(_text(node))


def _javadoc(node: Node) -> str | None:
    """The /** */ comment before the declaration (only other comments in between), or else one
    written between its annotations and the rest of it."""
    previous = node.prev_named_sibling
    while previous is not None and previous.type in _COMMENT_NODES:
        if _is_javadoc(previous):
            return _clean_javadoc(_text(previous))
        previous = previous.prev_named_sibling
    for child in node.children:
        if child.type == "modifiers":
            doc = next((c for c in child.children if _is_javadoc(c)), None)
            if doc is not None:
                return _clean_javadoc(_text(doc))
        elif _is_javadoc(child):
            return _clean_javadoc(_text(child))
        elif child.is_named and child.type not in _COMMENT_NODES:
            break
    return None


def _is_javadoc(node: Node) -> bool:
    if node.type != "block_comment":
        return False
    text = node.text or b""
    return text.startswith(b"/**") and text != b"/**/"


def _clean_javadoc(text: str) -> str | None:
    """Without the /** */ markers and the leading '*' of each line."""
    lines = []
    for line in text[3:-2].splitlines():
        line = line.lstrip()
        if line.startswith("*"):
            line = line[1:]
            if line.startswith(" "):
                line = line[1:]
        lines.append(line.rstrip())
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) or None


# ---- type references ---------------------------------------------------------------------


def _type_ref(node: Node, extra_dims: int = 0, varargs: bool = False) -> TypeRef:
    """A TypeRef for a type node. `extra_dims` are C-style dimensions written after the name
    (`int b[]`); `varargs` is `String...`."""
    kind = node.type
    if kind == "annotated_type":  # @NonNull String: the annotation is not part of the type
        inner = [c for c in _named(node) if c.type in _TYPE_NODES]
        if inner:
            return _type_ref(inner[-1], extra_dims, varargs)
    text = _compact(_text(node))
    name, args, dims = text, [], 0
    if kind == "generic_type":
        base = _first_child(node, {"type_identifier", "scoped_type_identifier"})
        name = _strip_generics(_squash(base)) if base is not None else text
        type_args = _first_child(node, {"type_arguments"})
        args = [_type_ref(arg) for arg in _named(type_args) if arg.type in _TYPE_NODES]
    elif kind == "array_type":
        element = node.child_by_field_name("element")
        dimensions = node.child_by_field_name("dimensions")
        if element is not None:
            inner = _type_ref(element)
            name, args, dims = inner.name, inner.args, inner.array_dims
        dims += (dimensions.text or b"").count(b"[") if dimensions is not None else 0
    elif kind == "wildcard":
        bound = [c for c in _named(node) if c.type in _TYPE_NODES]
        name, args = "?", [_type_ref(bound[-1])] if bound else []
    elif kind == "scoped_type_identifier":
        name = _strip_generics(_squash(node))
    if extra_dims:
        text += "[]" * extra_dims
        dims += extra_dims
    if varargs:
        text += "..."
        dims += 1
    return TypeRef(text=text, name=name, args=args, array_dims=dims)


def _dims(node: Node) -> int:
    """C-style dimensions after a name: `int b[]`, `String args[]`, `int f()[]`."""
    dimensions = node.child_by_field_name("dimensions")
    return (dimensions.text or b"").count(b"[") if dimensions is not None else 0


def _strip_generics(text: str) -> str:
    """'Outer<String>.Inner' -> 'Outer.Inner'."""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(depth - 1, 0)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


# ---- strings and text --------------------------------------------------------------------

_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", "r": "\r", "f": "\f", "s": " ", "0": "\0"}
_ESCAPE_RE = re.compile(r"\\(u+[0-9a-fA-F]{4}|[0-7]{1,3}|\r?\n|.)", re.DOTALL)


def _string_value(node: Node) -> str:
    """The value of a string literal or text block."""
    text = _text(node)
    if text.startswith('"""') and len(text) >= 6:
        return _unescape(_text_block(text[3:-3]))
    return _unescape(text[1:-1] if len(text) >= 2 else "")


def _text_block(content: str) -> str:
    """A text block's lines without the incidental indentation (JLS 3.10.6, simplified)."""
    lines = content.replace("\r\n", "\n").split("\n")[1:]  # the opening line is empty
    if not lines:
        return ""
    significant = [line for line in lines[:-1] if line.strip()] + [lines[-1]]
    indent = min(len(line) - len(line.lstrip()) for line in significant)
    stripped = [line[indent:].rstrip() for line in lines]
    return "\n".join(stripped)


def _unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape[0] == "u":
            return chr(int(escape.lstrip("u"), 16))
        if escape[0] in "01234567" and escape != "0":
            return chr(int(escape, 8))
        if escape in ("\n", "\r\n"):
            return ""  # a line continuation in a text block
        return _ESCAPES.get(escape, escape)

    return _ESCAPE_RE.sub(replace, text) if "\\" in text else text


# A text block, string or char literal; or a gap: whitespace and comments.
_TOKEN_RE = re.compile(
    r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
    r"|(?:\s|//[^\n]*|/\*[\s\S]*?\*/)+"
)


def _one_line(text: str) -> str:
    """Source text on one line, literals untouched: comments dropped, gaps collapsed to one
    space, and a line break before '.', ')' or ']' or after '(', '[' or '.' removed."""
    if not any(marker in text for marker in ("\n", "\r", "\t", "  ", "//", "/*")):
        return text

    def replace(match: re.Match[str]) -> str:
        gap = match.group()
        if gap[0] in "\"'":
            return gap
        start, end = match.start(), match.end()
        if start == 0 or end == len(text):
            return ""
        multiline = "\n" in gap or "\r" in gap
        if multiline and (text[start - 1] in "([." or text[end] in ".)]"):
            return ""
        return " "

    return _TOKEN_RE.sub(replace, text)


def _compact(text: str) -> str:
    """Whitespace runs as one space: for types written over several lines."""
    return re.sub(r"\s+", " ", text).strip() if any(c.isspace() for c in text) else text


def _squash(node: Node) -> str:
    """A dotted name without any whitespace: 'org.example . Foo' -> 'org.example.Foo'."""
    return re.sub(r"\s+", "", _text(node))


# ---- tree helpers ------------------------------------------------------------------------


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _end_line(node: Node) -> int:
    return node.end_point[0] + 1


def _named(node: Node | None) -> list[Node]:
    """Named children without comments."""
    if node is None:
        return []
    return [child for child in node.named_children if child.type not in _COMMENT_NODES]


def _first_child(node: Node | None, types: set[str]) -> Node | None:
    if node is None:
        return None
    return next((child for child in node.children if child.type in types), None)


def _ancestor(node: Node, types: set[str]) -> Node | None:
    parent = node.parent
    while parent is not None and parent.type not in types:
        if parent.type in _METHOD_NODES or parent.type == "lambda_expression":
            return None
        parent = parent.parent
    return parent
