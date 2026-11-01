# frozen_string_literal: true
#
# furnishings.rb - loose furniture drawn into SketchUp from a placement list, beside the house.
#
# bridge/furnishings.py checks furnishings/pieces.json and runs this file through the bridge's eval, holding the look
# lock around it:
#
#   load '/abs/project/bridge/furnishings.rb'; HouseTwin::Furnishings.run('<spec JSON>')
#
# Furniture is in no drawing and no photo, so it takes none of the house's pipeline: no house.json, no plan, no photo
# renders. It lives in its own top-level group, FURNISHINGS, which no build or sync touches and no photo render shows.
# Each piece is a group (a recipe) or a component instance (a .skp file) named furn/<id> in FURNISHINGS, tagged
# furnishings/<room> (a tag new to the model is created shown, in the tag folder "furnishings"), and carrying the
# attribute dictionary house_twin_furnishings: its id, its entry as placed and the transformation it was placed with.
#
# Commands (the spec's "cmd"):
# - place: every listed piece, or those "only" names. A piece FURNISHINGS lacks is created. A piece whose
#   transformation is still the one it was placed with is brought to its entry (redrawn when its recipe, size,
#   params, finish or the recipe's own code changed, moved when its place or rotation did, retagged when its tag did
#   unless the owner retagged it). A piece the owner has moved, turned or scaled by hand is left where they put it and
#   reported, as are copies of one (two furn/<id> of one id) and a furn/<id> this file did not place. Pieces in
#   FURNISHINGS the list does not name are reported, never removed.
# - remove: the pieces named, by id; nothing else.
# - list: what FURNISHINGS holds.
# Nothing outside FURNISHINGS is read or written but tags and materials. A tag the model has is never shown or hidden.
# Each place or remove is one operation, so the owner's undo takes it back in one step; a failure aborts it whole.
#
# Spec (a JSON string):
#
#   {"cmd": "place", "only": [],
#    "materials": {"wood_mid": {"rgb": [150, 111, 70]}, "glass": {"rgb": [200, 220, 230], "alpha": 0.2}, ...},
#    "pieces": [{"id": "sofa", "recipe": "sofa", "size": [84, 36, 32], "params": {"cushions": 3},
#                "finish": "fabric_gray", "at": [120.0, 60.0, 0.0], "rotation": 90.0, "tag": "furnishings/living"},
#               {"id": "lamp", "component": "/abs/project/furnishings/components/lamp.skp", "at": [...],
#                "rotation": 0.0, "tag": "furnishings/living"}]}
#   {"cmd": "remove", "ids": ["sofa"]}
#   {"cmd": "list"}
#
# "materials" gives the colour of every finish a piece may use; a material the model lacks is made from it, and one
# the model has is never changed. A piece's position is its origin in FURNISHINGS's frame, which is the house's while
# FURNISHINGS stands where it was made; rotation turns it about its origin, degrees counter-clockwise seen from above.
# A recipe's origin is its plan centre on the floor, its width along x, its depth along y and its front toward -y (a
# sofa's sitter faces -y; a bed's foot is at -y). A component's origin and axes are its own.
#
# The recipes are plain Ruby (Geometry and RECIPES): each a list of parts, a part a closed solid of planar faces wound
# counter-clockwise seen from outside, so they are tested outside SketchUp (tests/ruby/furnishings_rb.rb). Only the
# drawing and placing need SketchUp.

require 'digest'
require 'json'
require 'time'

module HouseTwin
  # Recreated on every load, so a hot reload leaves no stale methods or constants behind.
  remove_const(:Furnishings) if const_defined?(:Furnishings, false)

  module Furnishings
    class Error < StandardError; end

    TOP_NAME     = 'FURNISHINGS'
    PIECE_PREFIX = 'furn/'
    DICT         = 'house_twin_furnishings'
    FOLDER       = 'furnishings'
    ID_RE        = /\A[a-z][a-z0-9_]*\z/.freeze
    TAG_RE       = %r{\Afurnishings/[a-z][a-z0-9_]*\z}.freeze
    COMMANDS     = %w[place remove list].freeze
    MOVE_TOL     = 1e-3      # a transformation entry off by more than this is a hand move
    SMOOTH_DEG   = 35.0      # a smooth part's edges between faces nearer than this in angle are softened
    ROUND_SIDES  = 12        # the sides of a round member's section
    PILLOW_STEPS = 4         # the steps a rounded edge takes a quarter turn in

    # Each recipe's default size [width (x), depth (y), height], finish and params. A param ending in _finish is a
    # material key (a nil default follows the piece's finish); one in COUNTS is a whole number in its range; every
    # other param is a positive number of inches.
    RECIPES = {
      # A plain block: a dresser, a cabinet, a television, anything not yet worth a recipe of its own.
      'box' => { 'size' => [36.0, 18.0, 30.0], 'finish' => 'wood_mid', 'params' => {} },
      # A top on four square legs.
      'table' => { 'size' => [60.0, 36.0, 30.0], 'finish' => 'wood_mid',
                   'params' => { 'top' => 1.5,            # the top's thickness
                                 'leg' => 2.5,            # a leg's square section
                                 'inset' => 2.0 } },      # a leg's outer faces, in from the top's edges
      # A seat on four legs, the back legs running up to carry a back panel; height is the back's top.
      'chair' => { 'size' => [18.0, 20.0, 32.0], 'finish' => 'wood_mid',
                   'params' => { 'seat_height' => 18.0,   # the seat's top
                                 'seat' => 1.5,           # its thickness
                                 'leg' => 1.5,            # a leg's square section
                                 'back' => 1.0,           # the back panel's thickness
                                 'back_drop' => 12.0,     # the back panel's height, down from the chair's top
                                 'seat_finish' => nil } },   # the seat's material; nil: the chair's finish
      # An upholstered sofa: a base on round feet, a back and arms of one height, loose seat cushions and back
      # cushions leaning on the back; height is the back cushions' top.
      'sofa' => { 'size' => [84.0, 36.0, 32.0], 'finish' => 'fabric_gray',
                  'params' => { 'seat_height' => 18.0,    # the seat cushions' top
                                'arm_height' => 24.0,     # the arms' and the back's top
                                'arm' => 6.0,             # an arm's width
                                'back' => 7.0,            # the back's thickness
                                'back_cushion' => 8.0,    # how deep a back cushion's foot stands on the seat
                                'lean' => 3.0,            # how far a back cushion's top leans back past its foot
                                'leg_height' => 3.0,      # the feet, under the base
                                'base' => 6.0,            # the base under the seat cushions, over the feet
                                'cushions' => 3,          # seat cushions, and as many back cushions
                                'arm_left' => 1,          # an arm at -x (1) or none (0)
                                'arm_right' => 1,         # an arm at +x
                                'leg_finish' => 'wood_dark' } },
      # A bed: a frame with a headboard at +y, a mattress and pillows; depth is its length, height the headboard's top.
      'bed' => { 'size' => [60.0, 80.0, 44.0], 'finish' => 'wood_mid',
                 'params' => { 'frame_height' => 12.0,    # the frame's top, under the mattress
                               'mattress' => 10.0,        # the mattress's thickness
                               'headboard' => 2.0,        # the headboard's thickness
                               'pillows' => 2,
                               'bedding_finish' => 'fabric_white' } },
      # A rug: height is its thickness.
      'rug' => { 'size' => [96.0, 60.0, 0.5], 'finish' => 'fabric_rust', 'params' => {} },
    }.freeze
    COUNTS = { 'cushions' => 1..6, 'pillows' => 0..6, 'arm_left' => 0..1, 'arm_right' => 0..1 }.freeze

    # ------------------------------------------------------------------ geometry (plain Ruby)

    module Geometry
      module_function

      # A face's right-hand normal (Newell's), unnormalised: it points out of a solid for a face wound
      # counter-clockwise seen from outside.
      def normal(pts)
        n = [0.0, 0.0, 0.0]
        pts.each_with_index do |p, i|
          q = pts[(i + 1) % pts.length]
          n[0] += (p[1] - q[1]) * (p[2] + q[2])
          n[1] += (p[2] - q[2]) * (p[0] + q[0])
          n[2] += (p[0] - q[0]) * (p[1] + q[1])
        end
        n
      end

      # The faces of a solid lofted through stacked sections: planar polygons of one point count, each two of them
      # making planar quadrilaterals, wound counter-clockwise seen from the side the next section lies on.
      def loft(sections)
        n = sections.first.length
        raise Error, 'a loft needs two sections or more, of one point count' if sections.length < 2 ||
                                                                               sections.any? { |s| s.length != n }

        faces = [sections.first.reverse, sections.last]
        sections.each_cons(2) do |lo, hi|
          n.times { |k| faces << [lo[k], lo[(k + 1) % n], hi[(k + 1) % n], hi[k]] }
        end
        faces
      end

      # A horizontal rectangle at z, counter-clockwise seen from above.
      def rect(x0, y0, x1, y1, z)
        [[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]]
      end

      def box(x0, y0, z0, x1, y1, z1)
        loft([rect(x0, y0, x1, y1, z0), rect(x0, y0, x1, y1, z1)])
      end

      # A horizontal circle's polygon at z, counter-clockwise seen from above.
      def circle(cx, cy, d, z, sides = ROUND_SIDES)
        (0...sides).map do |k|
          a = 2.0 * Math::PI * (k + 0.5) / sides
          [cx + d / 2.0 * Math.cos(a), cy + d / 2.0 * Math.sin(a), z]
        end
      end

      def part(name, material, faces, smooth: false)
        { 'name' => name, 'material' => material, 'faces' => faces, 'smooth' => smooth }
      end

      # A plan outline with its corners rounded: `pts` its [u, v] corners counter-clockwise, `radii` each corner's
      # radius, the whole drawn in by `inset`. Each corner is an arc of PILLOW_STEPS chords about one centre whatever
      # the inset, so two outlines of one shape at two insets have their points alike and their chords parallel.
      def soft_outline(pts, radii, inset)
        n = pts.length
        (0...n).flat_map do |k|
          a, v, b = pts[k - 1], pts[k], pts[(k + 1) % n]
          d0, d1 = [[a, v], [v, b]].map do |from, to|
            len = Math.hypot(to[0] - from[0], to[1] - from[1])
            raise Error, 'an outline with two corners at one place' if len < 1e-9

            [(to[0] - from[0]) / len, (to[1] - from[1]) / len]
          end
          n0, n1 = [d0[1], -d0[0]], [d1[1], -d1[0]]                  # the two sides' outward normals
          turn = d0[0] * d1[1] - d0[1] * d1[0]
          raise Error, 'an outline with a corner that does not turn' if turn.abs < 1e-9

          s = turn.positive? ? 1.0 : -1.0                            # turning out, or in
          reach = radii[k] / (1.0 + n0[0] * n1[0] + n0[1] * n1[1])
          c = [v[0] - s * (n0[0] + n1[0]) * reach, v[1] - s * (n0[1] + n1[1]) * reach]
          rho = radii[k] - s * inset
          raise Error, "a corner rounded to #{radii[k]} in cannot be drawn in by #{inset}" unless rho.positive?

          a0 = Math.atan2(n0[1], n0[0])
          sweep = Math.atan2(turn, d0[0] * d1[0] + d0[1] * d1[1])
          (0..PILLOW_STEPS).map do |j|
            t = a0 + sweep * j / PILLOW_STEPS
            [c[0] + s * rho * Math.cos(t), c[1] + s * rho * Math.sin(t)]
          end
        end
      end

      # An upholstered slab: the outline `pts` with its corners rounded to `radii`, from t0 to t1 across it, its edges
      # at both ends rounded to r. The block gives each [u, v, t] its [x, y, z]; it must keep u, v, t a right hand.
      def soft_solid(pts, radii, t0, t1, r)
        raise Error, "a part #{(t1 - t0).round(3)} in thick is too thin for edges rounded to #{r} in" unless
          t1 - t0 > 2.0 * r

        turns = (0..PILLOW_STEPS).map { |k| Math::PI / 2.0 * k / PILLOW_STEPS }
        rings = turns.map { |t| [r * (1.0 - Math.sin(t)), t0 + r * (1.0 - Math.cos(t))] } +
                turns.reverse.map { |t| [r * (1.0 - Math.sin(t)), t1 - r * (1.0 - Math.cos(t))] }
        loft(rings.map { |i, t| soft_outline(pts, radii, i).map { |u, v| yield(u, v, t) } })
      end

      # An upholstered box: its upper and lower edges rounded to r and its upright ones a little more, its top
      # leaning `lean` toward +y over its foot.
      def pillow(x0, y0, z0, x1, y1, z1, r, lean: 0.0)
        rc = r * 1.25
        unless x1 - x0 >= 2.0 * rc && y1 - y0 >= 2.0 * rc && z1 - z0 > 2.0 * r
          raise Error, "a part #{(x1 - x0).round(3)} x #{(y1 - y0).round(3)} x #{(z1 - z0).round(3)} in is too " \
                       "small for edges rounded to #{r} in"
        end

        soft_solid([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], [rc] * 4, z0, z1, r) do |u, v, t|
          [u, v + lean * (t - z0) / (z1 - z0), t]
        end
      end

      # The softness that suits an upholstered part of the given smallest extent.
      def soft(*extents)
        [2.5, extents.min * 0.3].min
      end

      def box_piece(s, f, _p)
        w, d, h = s
        [part('body', f, box(-w / 2.0, -d / 2.0, 0.0, w / 2.0, d / 2.0, h))]
      end

      def table(s, f, p)
        w, d, h = s
        t, g, i = p['top'], p['leg'], p['inset']
        raise Error, "the table's top #{t} must be thinner than its height #{h}" unless t < h
        raise Error, "the table's legs (#{g} in, #{i} in in) do not fit under a #{w} x #{d} in top" unless
          2.0 * (g + i) < [w, d].min

        parts = [part('top', f, box(-w / 2.0, -d / 2.0, h - t, w / 2.0, d / 2.0, h))]
        [[-1, -1], [1, -1], [-1, 1], [1, 1]].each_with_index do |(sx, sy), k|
          x, y = sx * (w / 2.0 - i - g / 2.0), sy * (d / 2.0 - i - g / 2.0)
          parts << part("leg_#{k + 1}", f, box(x - g / 2.0, y - g / 2.0, 0.0, x + g / 2.0, y + g / 2.0, h - t))
        end
        parts
      end

      def chair(s, f, p)
        w, d, h = s
        sh, st, g, bt = p['seat_height'], p['seat'], p['leg'], p['back']
        raise Error, "the chair's seat #{st} must be thinner than its height #{sh}" unless st < sh
        raise Error, "the chair's back #{h} must stand over its seat #{sh} by its panel #{p['back_drop']}" unless
          h - p['back_drop'] > sh
        raise Error, "the chair's legs #{g} in do not fit under a #{w} x #{d} in seat" unless 2.0 * g + 2.0 < [w, d].min

        zu = sh - st
        xl, yf, yb = w / 2.0 - g / 2.0, -d / 2.0 + g / 2.0, d / 2.0 - g / 2.0
        parts = [part('seat', p['seat_finish'] || f, box(-w / 2.0, -d / 2.0, zu, w / 2.0, d / 2.0, sh))]
        [-1, 1].each_with_index do |sx, k|
          x = sx * xl
          parts << part("front_leg_#{k + 1}", f, box(x - g / 2.0, yf - g / 2.0, 0.0, x + g / 2.0, yf + g / 2.0, zu))
          parts << part("back_leg_#{k + 1}", f, box(x - g / 2.0, yb - g / 2.0, 0.0, x + g / 2.0, yb + g / 2.0, h))
        end
        parts << part('back', f, box(-xl + g / 2.0, yb - bt / 2.0, h - p['back_drop'], xl - g / 2.0, yb + bt / 2.0, h))
        parts
      end

      def sofa(s, f, p)
        w, d, h = s
        lh, zs, za = p['leg_height'], p['seat_height'], p['arm_height']
        zb = lh + p['base']
        raise Error, "the sofa's seat #{zs} must stand over its base #{zb} by 2 in or more" unless zs >= zb + 2.0
        raise Error, "the sofa's arms #{za} must stand between its seat #{zs} and its top #{h}" unless
          zs < za && za < h

        x0, x1, yf, yback = -w / 2.0, w / 2.0, -d / 2.0, d / 2.0
        xa = x0 + (p['arm_left'].positive? ? p['arm'] : 0.0)        # the seat between the arms' inner faces
        xb = x1 - (p['arm_right'].positive? ? p['arm'] : 0.0)
        yb = yback - p['back']                                       # the back's face
        n = p['cushions'].to_i
        raise Error, "#{n} cushions do not fit a seat #{(xb - xa).round(3)} in wide" unless (xb - xa) / n >= 12.0
        raise Error, "the back #{p['back']} and its cushions #{p['back_cushion']} leave no seat in a sofa #{d} " \
                     'in deep' unless yb - p['back_cushion'] - yf >= 12.0

        lf = p['leg_finish']
        parts = [[x0 + 4.0, yf + 4.0], [x1 - 4.0, yf + 4.0], [x0 + 4.0, yback - 4.0], [x1 - 4.0, yback - 4.0]]
                .each_with_index.map do |(x, y), k|
          part("foot_#{k + 1}", lf, loft([circle(x, y, 2.5, 0.0), circle(x, y, 2.5, lh)]), smooth: true)
        end
        parts << part('base', f, box(xa, yf + 1.0, lh, xb, yb, zb))
        parts << part('back', f, pillow(x0, yb, lh, x1, yback, za, soft(p['back'], w)), smooth: true)
        { 'arm_left' => [x0, xa], 'arm_right' => [xb, x1] }.each do |name, (a, b)|
          next unless b - a > 1e-6

          parts << part(name, f, pillow(a, yf, lh, b, yb, za, soft(b - a, za - lh)), smooth: true)
        end
        n.times do |k|
          a, b = xa + (xb - xa) * k / n, xa + (xb - xa) * (k + 1) / n
          parts << part("seat_cushion_#{k + 1}", f, pillow(a, yf, zb, b, yb, zs, soft(zs - zb, b - a)), smooth: true)
          # a back cushion leans back over the back's top, touching it only along that edge
          off = p['lean'] * (za - zs) / (h - zs)
          parts << part("back_cushion_#{k + 1}", f,
                        pillow(a, yb - p['back_cushion'], zs, b, yb - off, h, soft(p['back_cushion'] - off, b - a),
                               lean: p['lean']), smooth: true)
        end
        parts
      end

      def bed(s, f, p)
        w, d, h = s
        zf, mt, hb = p['frame_height'], p['mattress'], p['headboard']
        zm = zf + mt
        raise Error, "the bed's headboard #{h} must stand over its mattress #{zm}" unless h > zm + 2.0
        raise Error, "the headboard #{hb} leaves no bed #{d} in long" unless d - hb >= 24.0

        yh = d / 2.0 - hb
        b = p['bedding_finish']
        parts = [part('frame', f, box(-w / 2.0, -d / 2.0, 0.0, w / 2.0, yh, zf)),
                 part('headboard', f, box(-w / 2.0, yh, 0.0, w / 2.0, d / 2.0, h)),
                 part('mattress', b, pillow(-w / 2.0 + 0.5, -d / 2.0 + 0.5, zf, w / 2.0 - 0.5, yh - 0.5, zm,
                                            soft(mt, w)), smooth: true)]
        n = p['pillows'].to_i
        n.times do |k|
          span = (w - 4.0) / n
          a = -w / 2.0 + 2.0 + span * k
          parts << part("pillow_#{k + 1}", b, pillow(a + 1.0, yh - 22.0, zm, a + span - 1.0, yh - 2.0, zm + 5.0, 2.0),
                        smooth: true)
        end
        parts
      end

      def rug(s, f, _p)
        box_piece(s, f, nil).map { |q| q.merge('name' => 'rug') }
      end

      DRAW = { 'box' => :box_piece, 'table' => :table, 'chair' => :chair, 'sofa' => :sofa, 'bed' => :bed,
               'rug' => :rug }.freeze

      # A recipe's parts for a size, finish and params over its defaults (Furnishings.settings checks them).
      def recipe(name, size = nil, finish = nil, params = {})
        s, f, p = Furnishings.settings(name, size, finish, params)
        send(DRAW.fetch(name), s, f, p)
      end
    end

    class << self
      # A recipe's size, finish and params over its defaults, checked.
      def settings(recipe, size, finish, given)
        defaults = RECIPES[recipe] || raise(Error, "no recipe #{recipe.inspect}; the recipes are #{RECIPES.keys.join(' ')}")
        raise Error, "#{recipe}'s params must be an object" unless given.is_a?(Hash)

        s = size || defaults['size']
        unless s.is_a?(Array) && s.length == 3 && s.all? { |v| v.is_a?(Numeric) && v.to_f.finite? && v.positive? }
          raise Error, "#{recipe}'s size must be three positive numbers [width, depth, height], got #{s.inspect[0, 60]}"
        end
        f = finish || defaults['finish']
        raise Error, "#{recipe}'s finish must be a material key, got #{f.inspect[0, 40]}" unless f.is_a?(String) && !f.empty?

        given.each do |k, v|
          raise Error, "#{recipe} takes no param #{k.inspect}; its params are #{defaults['params'].keys.join(' ')}" unless
            defaults['params'].key?(k)

          if k.end_with?('_finish')
            raise Error, "#{recipe} param #{k} must be a material key, got #{v.inspect[0, 40]}" unless v.is_a?(String) && !v.empty?
          elsif COUNTS.key?(k)
            raise Error, "#{recipe} param #{k} must be a whole number in #{COUNTS[k]}, got #{v.inspect[0, 40]}" unless
              v.is_a?(Numeric) && v == v.to_i && COUNTS[k].cover?(v.to_i)
          elsif !(v.is_a?(Numeric) && v.to_f.finite? && v.positive?)
            raise Error, "#{recipe} param #{k} must be a positive number of inches, got #{v.inspect[0, 40]}"
          end
        end
        [s.map(&:to_f), f, defaults['params'].merge(given)]
      end

      # ------------------------------------------------------------ the spec

      def read(spec_json)
        raw = begin
          spec_json.is_a?(Hash) ? spec_json : JSON.parse(spec_json.to_s)
        rescue JSON::ParserError => e
          raise Error, "the spec is not valid JSON: #{e.message[0, 200]}"
        end
        raise Error, 'the spec must be a JSON object' unless raw.is_a?(Hash)

        cmd = raw['cmd']
        raise Error, "cmd must be one of #{COMMANDS.join(' ')}, got #{cmd.inspect[0, 40]}" unless COMMANDS.include?(cmd)

        ids = strings(raw, 'ids').each { |i| piece_id(i) }
        only = strings(raw, 'only').each { |i| piece_id(i) }
        pieces = list(raw, 'pieces').each_with_index.map { |p, i| piece(p, "pieces[#{i}]") }
        twice = pieces.map { |p| p[:id] }.tally.select { |_, n| n > 1 }.keys
        raise Error, "each piece is listed once; repeated: #{twice.join(' ')}" unless twice.empty?

        materials = raw.fetch('materials', {})
        raise Error, 'materials must be an object of colours by key' unless materials.is_a?(Hash)

        { cmd: cmd, ids: ids, only: only, pieces: pieces, materials: materials }
      end

      # An id, as a piece's group name takes it after furn/: anything else is refused, so no command can reach a group
      # of another name.
      def piece_id(v)
        raise Error, "a piece id is lowercase letters, digits and _, starting with a letter, got #{v.inspect[0, 60]}" \
          unless v.is_a?(String) && ID_RE.match?(v)

        v
      end

      def piece(p, where)
        raise Error, "#{where} must be an object" unless p.is_a?(Hash)

        id = piece_id(p['id'])
        raise Error, "#{where} (#{id}) names a recipe or a component, one of the two" unless
          p.key?('recipe') ^ p.key?('component')

        at = p['at']
        unless at.is_a?(Array) && at.length == 3 && at.all? { |x| x.is_a?(Numeric) && x.to_f.finite? }
          raise Error, "#{where} (#{id}) at must be three numbers, got #{at.inspect[0, 60]}"
        end
        rot = p.fetch('rotation', 0.0)
        raise Error, "#{where} (#{id}) rotation must be a number of degrees" unless rot.is_a?(Numeric) && rot.to_f.finite?

        tag = p['tag']
        raise Error, "#{where} (#{id}) tag must be furnishings/<room>, got #{tag.inspect[0, 60]}" unless
          tag.is_a?(String) && TAG_RE.match?(tag)

        out = { id: id, at: at.map(&:to_f), rotation: rot.to_f, tag: tag }
        if p.key?('recipe')
          out[:recipe] = p['recipe']
          out[:size], out[:finish], out[:params] = settings(p['recipe'], p['size'], p['finish'], p.fetch('params', {}))
        else
          c = p['component']
          unless c.is_a?(String) && File.absolute_path?(c) && c.downcase.end_with?('.skp')
            raise Error, "#{where} (#{id}) component must be an absolute path to a .skp file, got #{c.inspect[0, 120]}"
          end

          out[:component] = c
        end
        out
      end

      def strings(raw, key)
        v = raw.fetch(key, [])
        raise Error, "#{key} must be a list of strings" unless v.is_a?(Array) && v.all? { |x| x.is_a?(String) }

        v
      end

      def list(raw, key)
        v = raw.fetch(key, [])
        raise Error, "#{key} must be a list" unless v.is_a?(Array)

        v
      end

      # A piece's entry as recorded and compared: what the list says of it as one canonical JSON text, and for a
      # recipe a digest of the parts it drew, so a change to a recipe's code redraws its pieces on the next place.
      def entry_json(p, parts = nil)
        h = { 'id' => p[:id], 'at' => p[:at], 'rotation' => p[:rotation], 'tag' => p[:tag] }
        if p[:recipe]
          h.update('recipe' => p[:recipe], 'size' => p[:size], 'finish' => p[:finish], 'params' => p[:params].sort.to_h)
        end
        h['draw'] = Digest::MD5.hexdigest(JSON.generate(parts)) if parts
        h['component'] = p[:component] if p[:component]
        JSON.generate(h)
      end

      # What place does with a listed piece: :create when FURNISHINGS has no furn/<id>; :copies for more than one;
      # :foreign for one this file did not place; :moved when its transformation (16 numbers) is not the one it was
      # placed with; :unchanged when its recorded entry is the list's; :update otherwise.
      def decide(count, recorded, entry, current)
        return :create if count.zero?
        return :copies if count > 1
        return :foreign unless recorded && recorded['placed'] && recorded['entry']
        return :moved if moved?(recorded['placed'], current)
        return :unchanged if recorded['entry'] == entry

        :update
      end

      def moved?(placed, current)
        placed = JSON.parse(placed) if placed.is_a?(String)
        return true unless placed.is_a?(Array) && placed.length == current.length

        placed.zip(current).any? { |a, b| (a.to_f - b.to_f).abs > MOVE_TOL }
      end

      # What changes the drawing, not the placing.
      def geometry_of(entry)
        JSON.parse(entry).slice('recipe', 'size', 'finish', 'params', 'component', 'draw')
      rescue JSON::ParserError
        {}
      end

      # ------------------------------------------------------------ SketchUp

      def run(spec_json)
        spec = read(spec_json)
        model = Sketchup.active_model
        raise Error, 'a group or component is open for editing in SketchUp; close it first' if model.active_path

        case spec[:cmd]
        when 'place' then place(model, spec)
        when 'remove' then remove(model, spec[:ids])
        else holdings(model)
        end
      end

      def top_group(model)
        tops = model.entities.grep(Sketchup::Group).select { |g| g.valid? && g.name == TOP_NAME }
        raise Error, "the model holds #{tops.length} top-level #{TOP_NAME} groups; keep one" if tops.length > 1

        tops.first
      end

      def piece_entity?(e)
        (e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance)) && e.valid? && e.name.start_with?(PIECE_PREFIX)
      end

      # FURNISHINGS's pieces by id: {id => [entity, ...]}.
      def pieces_in(top)
        return {} unless top

        top.entities.to_a.select { |e| piece_entity?(e) }.group_by { |e| e.name.delete_prefix(PIECE_PREFIX) }
      end

      def recorded(e)
        { 'entry' => e.get_attribute(DICT, 'entry'), 'placed' => e.get_attribute(DICT, 'placed') }
      end

      def transform(p)
        Geom::Transformation.translation(Geom::Vector3d.new(*p[:at])) *
          Geom::Transformation.rotation(Geom::Point3d.new(0, 0, 0), Geom::Vector3d.new(0, 0, 1),
                                        p[:rotation] * Math::PI / 180.0)
      end

      def identity?(t)
        t.to_a.zip(Geom::Transformation.new.to_a).all? { |a, b| (a - b).abs <= MOVE_TOL }
      end

      def place(model, spec)
        listed = spec[:pieces].map { |p| p[:id] }
        unknown = spec[:only] - listed
        raise Error, "only names pieces the list does not: #{unknown.join(' ')}" unless unknown.empty?

        todo = spec[:only].empty? ? spec[:pieces] : spec[:pieces].select { |p| spec[:only].include?(p[:id]) }
        # everything that can refuse, before anything changes
        parts = todo.select { |p| p[:recipe] }.to_h { |p| [p[:id], Geometry.recipe(p[:recipe], p[:size], p[:finish], p[:params])] }
        keys = parts.values.flatten.map { |q| q['material'] }.uniq
        missing = keys.reject { |k| model.materials[k] || spec[:materials][k] }
        raise Error, "no colour for the materials #{missing.join(' ')}: name palette keys or fabrics" unless missing.empty?

        gone = todo.select { |p| p[:component] && !File.file?(p[:component]) }.map { |p| p[:component] }
        raise Error, "no component file #{gone.join(' ')}" unless gone.empty?

        top = top_group(model)
        found = pieces_in(top)
        out = { 'created' => [], 'updated' => [], 'unchanged' => [], 'moved' => [], 'copies' => [], 'foreign' => [],
                'not_listed' => (found.keys - listed).sort, 'tags_created' => [], 'materials_created' => [] }
        owner_tag = model.active_layer
        untagged = model.layers[0]
        model.start_operation('Furnishings: place', true)
        begin
          # new entities take the active tag; keep them untagged whatever tag the owner has made current
          model.active_layer = untagged unless owner_tag == untagged
          mats = keys.to_h { |k| [k, model.materials[k] || material(model, k, spec[:materials][k], out)] }
          unless top
            top = model.entities.add_group
            top.name = TOP_NAME
            out['furnishings_created'] = true
          end
          todo.each do |p|
            have = found[p[:id]] || []
            e = have.first
            entry = entry_json(p, parts[p[:id]])
            case decide(have.length, e && recorded(e), entry, e&.transformation&.to_a)
            when :create
              e = p[:recipe] ? draw_piece(top, p, parts[p[:id]], mats) : add_component(model, top, p)
              e.layer = tag(model, p[:tag], out)
              stamp(e, p, entry)
              out['created'] << p[:id]
            when :update
              update(model, top, e, p, entry, parts[p[:id]], mats, out)
              out['updated'] << p[:id]
            when :unchanged then out['unchanged'] << p[:id]
            when :moved then out['moved'] << p[:id]
            when :copies then out['copies'] << p[:id]
            else out['foreign'] << p[:id]
            end
          end
          model.active_layer = owner_tag unless model.active_layer == owner_tag
          model.commit_operation
        rescue Exception # rubocop:disable Lint/RescueException
          model.abort_operation
          model.active_layer = owner_tag if owner_tag.valid? && model.active_layer != owner_tag
          raise
        end
        out['furnishings_moved'] = !identity?(top.transformation)
        out
      end

      # A listed piece still where it was placed, brought to the list's entry: redrawn in place when its drawing
      # changed (made again when it changes between a recipe and a component or takes another component), moved, and
      # retagged unless the owner retagged it.
      def update(model, top, e, p, entry, parts, mats, out)
        was = e.get_attribute(DICT, 'entry')
        tag_was = begin
          JSON.parse(was)['tag']
        rescue JSON::ParserError
          nil
        end
        retag = e.layer.name == tag_was && tag_was != p[:tag]
        if geometry_of(was) != geometry_of(entry)
          if e.is_a?(Sketchup::Group) && p[:recipe]
            e.make_unique
            e.entities.clear!
            draw_parts(e.entities, parts, mats)
          else
            layer = e.layer
            e.erase!
            e = p[:recipe] ? draw_piece(top, p, parts, mats) : add_component(model, top, p)
            e.layer = layer
          end
        end
        e.transformation = transform(p)
        e.layer = tag(model, p[:tag], out) if retag
        stamp(e, p, entry)
      end

      def stamp(e, p, entry)
        e.set_attribute(DICT, 'id', p[:id])
        e.set_attribute(DICT, 'entry', entry)
        e.set_attribute(DICT, 'placed', JSON.generate(e.transformation.to_a))
        e.set_attribute(DICT, 'placed_at', Time.now.utc.iso8601)
      end

      def draw_piece(top, p, parts, mats)
        g = top.entities.add_group
        g.name = "#{PIECE_PREFIX}#{p[:id]}"
        draw_parts(g.entities, parts, mats)
        g.transformation = transform(p)
        g
      end

      def add_component(model, top, p)
        defn = model.definitions.find { |d| d.path == p[:component] } || model.definitions.load(p[:component])
        inst = top.entities.add_instance(defn, transform(p))
        inst.name = "#{PIECE_PREFIX}#{p[:id]}"
        inst
      end

      # Each part a group of its own, so solids never merge where they meet, its faces in the part's material and
      # turned outward as wound (SketchUp turns a face on z 0 down whatever its winding), a round part's edges
      # softened.
      def draw_parts(entities, parts, mats)
        parts.each do |q|
          g = entities.add_group
          g.name = q['name']
          q['faces'].each do |pts|
            f = g.entities.add_face(pts.map { |x| Geom::Point3d.new(*x) })
            raise Error, "#{q['name']}: SketchUp made no face of #{pts.length} points" unless f

            f.reverse! if f.normal.dot(Geom::Vector3d.new(*Geometry.normal(pts))).negative?
            f.material = mats[q['material']]
          end
          soften(g.entities) if q['smooth']
        end
      end

      def soften(entities)
        limit = SMOOTH_DEG * Math::PI / 180.0
        entities.grep(Sketchup::Edge).each do |e|
          fs = e.faces
          next unless fs.length == 2 && fs[0].normal.angle_between(fs[1].normal) < limit

          e.soft = true
          e.smooth = true
        end
      end

      # A material the model lacks, made from the spec's colour for its key; one the model has is never changed.
      def material(model, key, colour, out)
        m = model.materials.add(key)
        rgb = colour['rgb'] || [255, 0, 255]
        m.color = Sketchup::Color.new(*rgb.map(&:to_i))
        m.alpha = colour['alpha'].to_f if colour['alpha']
        out['materials_created'] << key
        m
      end

      # A furnishings tag: the model's, as the owner has it, or a new one, shown, in the tag folder "furnishings".
      def tag(model, name, out)
        layers = model.layers
        layer = layers[name]
        return layer if layer

        layer = layers.add(name)
        if layers.respond_to?(:folders) && layer.respond_to?(:folder=)
          folder = layers.folders.find { |x| x.name == FOLDER } || layers.add_folder(FOLDER)
          layer.folder = folder
        end
        out['tags_created'] << name
        layer
      end

      def remove(model, ids)
        found = pieces_in(top_group(model))
        out = { 'removed' => [], 'not_found' => ids.reject { |i| found.key?(i) } }
        return out if (ids & found.keys).empty?

        model.start_operation('Furnishings: remove', true)
        begin
          ids.each do |i|
            next unless found[i]

            found[i].each(&:erase!)
            out['removed'] << i
          end
          model.commit_operation
        rescue Exception # rubocop:disable Lint/RescueException
          model.abort_operation
          raise
        end
        out
      end

      # What FURNISHINGS holds: each group's and component's name, kind and tag, and for a piece its id and whether it
      # has been moved by hand since it was placed (nil for one this file did not place). Nothing here writes.
      def holdings(model)
        top = top_group(model)
        return { 'furnishings' => false, 'entities' => [] } unless top

        solid = top.entities.to_a.select { |e| (e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance)) && e.valid? }
        rows = solid.map do |e|
          row = { 'name' => e.name, 'kind' => e.typename, 'tag' => e.layer.name, 'tag_visible' => e.layer.visible? }
          if piece_entity?(e)
            r = recorded(e)
            row['id'] = e.name.delete_prefix(PIECE_PREFIX)
            row['at'] = e.transformation.to_a[12, 3]
            row['moved'] = r['placed'] ? moved?(r['placed'], e.transformation.to_a) : nil
          end
          row
        end
        { 'furnishings' => true, 'hidden' => top.hidden?, 'moved' => !identity?(top.transformation),
          'entities' => rows, 'loose' => top.entities.length - solid.length }
      end
    end
  end
end
