import cobble.env
import cobble.target
import os.path
from itertools import chain
from cobble.plugin import *

DEPS_INCLUDE_SYSTEM = cobble.env.overrideable_bool_key(
    name = 'c_deps_include_system',
    default = True,
    readout = lambda x: '-MD' if x else '-MMD',
    help = ('Whether to recompile in response to changes to system headers. '
            '(Default: True)'),
)
LINK_SRCS = cobble.env.prepending_string_seq_key('c_link_srcs',
        help = ('Accumulates objects and archives for the link process. '
                'Items added to this key are *prepended* to reflect the fact '
                'that we visit the DAG in post-order, but C linkers expect '
                'pre-order.'))
LINK_FLAGS = cobble.env.appending_string_seq_key('c_link_flags',
        help = 'Extra flags to pass to cxx when used as linker.')
CC = cobble.env.overrideable_string_key('cc',
        help = 'Path to the C compiler to use.')
CXX = cobble.env.overrideable_string_key('cxx',
        help = 'Path to the C++ compiler to use (also used for link).')
ASPP = cobble.env.overrideable_string_key('aspp',
        help = 'Path to the program to use to process .S files (often cc).')
AR = cobble.env.overrideable_string_key('ar',
        help = 'Path to the system library archiver.')
C_FLAGS = cobble.env.appending_string_seq_key('c_flags',
        help = 'Extra flags to pass to cc for C targets.')
CXX_FLAGS = cobble.env.appending_string_seq_key('cxx_flags',
        help = 'Extra flags to pass to cxx for C++ targets.')
ASPP_FLAGS = cobble.env.appending_string_seq_key('aspp_flags',
        help = 'Extra flags to pass to aspp for .S targets.')
ARCHIVE_PRODUCTS = cobble.env.overrideable_bool_key(
        'c_library_archive_products',
        default = True,
        help = ('Whether to internally produce .a archives for libraries. '
                'When True (the default), c_library targets will produce '
                'a static archive using the configured ar tool, and users '
                'will depend on the archive. When False, users will depend '
                'directly on the bag of objects produced when compiling '
                'the library. The default setting produces slightly slower '
                'builds with more readable command lines.'))
WHOLE_ARCHIVE = cobble.env.overrideable_bool_key('c_library_whole_archive',
        help = ('Whether to force inclusion of all of a library at link. '
                'This would normally be set in the local delta of a '
                'c_library target that needs to alter the default linker '
                'behavior by adding --whole-archive. This is useful for '
                'things like interrupt vector tables that might not appear '
                '"used" otherwise, but should be left False (the default) '
                'for most libraries.'))

KEYS = frozenset([DEPS_INCLUDE_SYSTEM, LINK_SRCS, LINK_FLAGS, CC, CXX,
    C_FLAGS, CXX_FLAGS, ASPP, AR, ASPP_FLAGS, ARCHIVE_PRODUCTS,
    WHOLE_ARCHIVE])

_compile_keys = frozenset([cobble.target.ORDER_ONLY.name, DEPS_INCLUDE_SYSTEM.name])
_link_keys = frozenset([cobble.target.IMPLICIT.name, CXX.name, LINK_SRCS.name,
    LINK_FLAGS.name])
_archive_keys = frozenset([AR.name])

@target_def
def c_binary(package, name, *,
        env,
        deps = [],
        sources = [],
        local: Delta = {},
        extra: Delta = {}):

    def mkusing(ctx):
        # Allow environment key interpolation in source names
        sources_i = ctx.rewrite_sources(sources)
        # Generate object file products for all sources.
        objects = [_compile_object(package, s, ctx.env) for s in sources_i]
        # Extract just the output paths
        obj_files = list(chain(*[prod.outputs for prod in objects]))
        # Create the environment used for the linked product. Note that the
        # source files specific to this target, which we have just handled
        # above, are being included in both the link sources and the implicit
        # deps. An alternative would have been to provide them as inputs, but
        # this causes them not to contribute to the program's environment hash,
        # which would be Bad.
        program_env = ctx.env.subset_require(_link_keys).derive({
            LINK_SRCS.name: obj_files,
            '__implicit__': obj_files,
        })
        # Construct the actual linked program product.
        program_path = package.outpath(program_env, name)
        program = cobble.target.Product(
            env = program_env,
            outputs = [program_path],
            rule = 'link_c_program',
        )
        program.expose(path = program_path, name = name)
        program.symlink(target = program_path, source = package.linkpath(name))

        # TODO: this is really just a way of naming the most derived node in
        # the build graph we just emitted, so that our users can depend on just
        # it. This could be factored out.
        using = {
            '__implicit__': [package.linkpath(name)],
        }

        products = objects + [program]
        return (using, products)

    return cobble.target.Target(
        package = package,
        name = name,
        concrete = True,
        down = lambda _up_unused: package.project.named_envs[env].derive(extra),
        using_and_products = mkusing,
        local = local,
        deps = deps,
    )

@target_def
def c_library(package, name, *,
        deps = [],
        sources = [],
        local: Delta = {},
        using: Delta = {}):
    _using = using # free up name

    def mkusing(ctx):
        # Allow environment key interpolation in source names
        sources_i = ctx.rewrite_sources(sources)
        # Generate object file products for all sources.
        objects = [_compile_object(package, s, ctx.env) for s in sources_i]
        # Extract just the output paths
        obj_files = list(chain(*[prod.outputs for prod in objects]))

        # We have two modes for creating libraries: we can ar them, or not.
        if ctx.env[ARCHIVE_PRODUCTS.name] and obj_files:
            # We only have one output, a static library.
            outs = [package.outpath(ctx.env, 'lib' + name + '.a')]
            # Prepare environment for ar, being sure to include the object files
            # (and thus their hashes). The ar rule will not *consume* `link_srcs`.
            ar_env = ctx.env.subset_require(_archive_keys).derive({
                LINK_SRCS.name: obj_files,
            })
            library = [cobble.target.Product(
                env = ar_env,
                outputs = outs,
                rule = 'archive_c_library',
                inputs = obj_files,
            )]

            if ctx.env[WHOLE_ARCHIVE.name]:
                link_srcs = ['-Wl,-whole-archive'] + outs + ['-Wl,-no-whole-archive']
            else:
                link_srcs = outs
        else:
            # We'll provide a bag of .o files to our users.
            outs = obj_files
            link_srcs = obj_files
            library = []

        using = (
            _using,
            cobble.env.prepare_delta({
                # Cause our users to implicitly pick up dependence on our objects.
                '__implicit__': outs,
                # And also to link them in.
                LINK_SRCS.name: outs,
            }),
        )
        products = objects + library
        return (using, products)

    return cobble.target.Target(
        package = package,
        name = name,
        using_and_products = mkusing,
        deps = deps,
        local = local,
    )

_file_type_map = {
    '.c': ('compile_c_obj', [CC.name, C_FLAGS.name]),
    '.cc': ('compile_cxx_obj', [CXX.name, CXX_FLAGS.name]),
    '.cpp': ('compile_cxx_obj', [CXX.name, CXX_FLAGS.name]),
    '.S': ('assemble_obj_pp', [ASPP.name, ASPP_FLAGS.name]),
}

# Common factor of targets that compile C code.
def _compile_object(package, source, env):
    ext = os.path.splitext(source)[1]
    rule, keys = _file_type_map[ext]
    # add in the global compile keys
    keys = _compile_keys | frozenset(keys)

    o_env = env.subset_require(keys)
    # Shorten source names, in case we're using an output as input.
    src = os.path.basename(source)
    return cobble.target.Product(
        env = o_env,
        outputs = [package.outpath(o_env, src + '.o')],
        rule = rule,
        inputs = [source]
    )

ninja_rules = {
    'compile_c_obj': {
        'command': '$cc $c_deps_include_system -MF $depfile $c_flags -c -o $out $in',
        'description': 'C $in',
        'depfile': '$out.d',
        'deps': 'gcc',
    },
    'compile_cxx_obj': {
        'command': '$cxx $c_deps_include_system -MF $depfile $cxx_flags -c -o $out $in',
        'description': 'CXX $in',
        'depfile': '$out.d',
        'deps': 'gcc',
    },
    'assemble_obj_pp': {
        'command': '$aspp $c_deps_include_system -MF $depfile $aspp_flags -c -o $out $in',
        'description': 'AS+CPP $in',
        'depfile': '$out.d',
        'deps': 'gcc',
    },
    'link_c_program': {
        'command': '$cxx $c_link_flags -o $out $in $c_link_srcs',
        'description': 'LINK $out',
    },
    'archive_c_library': {
        'command': '$ar rcs $out $in',
        'description': 'AR $out',
    },
}
