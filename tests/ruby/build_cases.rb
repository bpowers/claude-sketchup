# frozen_string_literal: true
#
# build.rb's and render.rb's bookkeeping under plain Ruby, against tests/ruby/sketchup_stub.rb.
#
#   ruby tests/ruby/build_cases.rb config.json
#
# tests/test_ruby.py writes config.json (the paths of build.rb, plans, manifests and malformed inputs) and asserts on
# the one JSON object this prints. Drawing is stubbed (each element's builder adds one empty part, creases are none),
# so what runs is build.rb's own reading of palettes, plans and manifests, its tags and materials, the in-place
# placement of pieces under chains, choose, and render.rb's request parsing and area switching.

require 'json'
require_relative 'sketchup_stub'

config = JSON.parse(File.read(ARGV.fetch(0)))
load config.fetch('build_rb')

# rubocop:disable Style/GlobalVars
module HouseTwin
  module Build
    class << self
      %w[space slab wall column beam stair box roof_plane].each do |t|
        define_method("build_#{t}") { |model, g, _el, _mats, _stats| part(model, g, 'body') }
      end

      def draw_creases(*)
        []
      end

      def element_report(g, chain = [])
        { 'id' => element_id(g), 'chain' => chain, 'faces' => 0 }
      end

      def write_report(_stats, _rows, _creases, path = REPORT)
        $reports << path
      end
    end
  end
end

def error_of
  yield
  nil
rescue HouseTwin::BuildError => e
  e.message
end

# Every group under g as "name [tag]" paths joined with " > ", an element group by its recorded id (an instance
# shown under a second chain has no name of its own).
def tree(g, above = [])
  g.entities.grep(Sketchup::Group).flat_map do |x|
    here = above + ["#{x.get_attribute('house_twin', 'id') || x.name} [#{x.layer.name}]"]
    [here.join(' > '), *tree(x, here)]
  end
end

def tops(model)
  model.entities.grep(Sketchup::Group).map(&:name)
end

def sync_row(model, stats)
  house = HouseTwin::Build.house_group(model)
  keep = %w[in_place built instanced kept erased placements]
  { 'stats' => stats.select { |k, _| keep.include?(k) }, 'tree' => tree(house),
    'tags' => model.layers.to_h { |l| [l.name, l.visible?] }, 'active' => model.active_layer.name,
    'created_on' => Sketchup.created.uniq, 'ops' => model.operations.dup,
    'identity' => HouseTwin::Build.identity(model), 'location' => model.shadow_info.dup,
    'moved_containers' => stats['moved_containers'] }
end

B = HouseTwin::Build
R = HouseTwin::Render
out = {}
$reports = []

# ---------------------------------------------------------------- palette and materials
model = Sketchup.active_model = Sketchup::Model.new
pal = B.read_palette(config['palette'])
mats = B.ensure_materials(model, pal)
B.ensure_materials(model, pal)
out['palette'] = { 'keys' => pal.keys.length, 'glass_alpha' => mats['glass'].alpha,
                   'drywall_rgb' => mats['drywall'].color.to_a[0, 3], 'color_sets' => mats['drywall'].sets,
                   'bad' => config['bad_palettes'].map { |p| error_of { B.read_palette(p) } } }

# ---------------------------------------------------------------- plans and manifests
plan = B.read_plan(config['plan'])
out['plan'] = { 'elements' => plan[:elements].length, 'first_keys' => plan[:elements][0].keys.first(4).map(&:inspect),
                'location' => plan[:location], 'zones' => plan[:zones].keys.map(&:to_s),
                'not_a_plan' => error_of { B.read_plan(config['house_json']) },
                'missing' => error_of { B.read_plan('/nonexistent/plan.json') } }
out['manifest'] = { 'ok' => B.read_manifest(config['manifests']['a'])[1]['manifest'],
                    'bad' => config['bad_manifests'].map { |p| error_of { B.read_manifest(p) } } }

# ---------------------------------------------------------------- sync, in place
model = Sketchup.active_model = Sketchup::Model.new
owner = B.tag(model, 'mine')
model.active_layer = owner
rows = {}
rows['first'] = sync_row(model, B.sync(config['manifests']['a']))
rows['again'] = sync_row(model, B.sync(config['manifests']['a']))
model.layers['kitchen/island'].visible = true    # the owner switches the kitchen by hand
model.layers['kitchen/today'].visible = false
Sketchup.created.clear
rows['changed'] = sync_row(model, B.sync(config['manifests']['b']))
rows['back'] = sync_row(model, B.sync(config['manifests']['a']))
# The owner moves a chain container and a zone group by hand, and deletes the tag of a chain container, which moves its
# groups to Untagged: a sync builds the moved ones' pieces again and puts the untagged container back on its tag.
house = B.house_group(model)
living = house.entities.grep(Sketchup::Group).find { |g| g.name == 'zone/living' }
living.entities.grep(Sketchup::Group).find { |g| g.name == 'kitchen/today' }.layer = model.layers[0]
house.entities.grep(Sketchup::Group).find { |g| g.name == 'zone/kitchen' }.transformation = Geom::Transformation.new(true)
rows['settled'] = sync_row(model, B.sync(config['manifests']['a']))
living.entities.grep(Sketchup::Group).find { |g| g.name == 'kitchen/today' }.transformation = Geom::Transformation.new(true)
rows['moved_chain'] = sync_row(model, B.sync(config['manifests']['a']))
rows['rebuild'] = sync_row(model, B.sync(config['manifests']['a'], rebuild: true))
rows['furnishings'] = error_of { B.run(config['plan'], top_name: 'FURNISHINGS') }
out['sync'] = rows

# ---------------------------------------------------------------- run into a test's top group
model.shadow_info.clear
$reports.clear
stats = B.run(config['plan'], top_name: 'TEST top')
out['run'] = { 'tops' => tops(model), 'elements' => stats['elements'], 'built' => stats['built'],
               'in_place' => stats['in_place'], 'location' => model.shadow_info.dup, 'reports' => $reports.dup,
               'house_kept' => !B.house_group(model).nil?,
               'test_tree' => tree(B.house_group(model, 'TEST top')).first(3) }

# ---------------------------------------------------------------- choose
before = model.operations.length
chosen = B.choose({ 'kitchen' => 'today' }, model)
out['choose'] = { 'on' => chosen, 'tags' => %w[kitchen/today kitchen/island].to_h { |t| [t, model.layers[t].visible?] },
                  'ops' => model.operations[before..],
                  'unknown_area' => error_of { B.choose({ 'garage' => 'today' }, model) },
                  'unknown_alt' => error_of { B.choose({ 'kitchen' => 'galley' }, model) },
                  'agree' => [B.chains_agree?({ 'kitchen' => 'kitchen/today' }, { 'kitchen' => 'kitchen/today' }),
                              B.chains_agree?({ 'kitchen' => 'kitchen/today' }, { 'kitchen' => 'kitchen/island' }),
                              B.chains_agree?({ 'kitchen' => 'kitchen/today' }, { 'dining' => 'dining/bench' })] }

# ---------------------------------------------------------------- render requests
base = { 'out' => '/tmp/x.png', 'width' => 1600, 'height' => 1067, 'eye' => [0, -200, 60], 'target' => [0, 0, 60],
         'up' => [0, 0, 1], 'fov_v_deg' => 55.0, 'style' => 'edges' }
parsed = R.read(base.merge('hide_tags' => ['zone/roof'], 'areas' => ['kitchen/island'], 'crop' => [0, 0, 10, 10]))
out['render_read'] = {
  'ok' => parsed.transform_keys(&:to_s).slice('projection', 'fov', 'style', 'transparent', 'hide_tags', 'areas', 'top',
                                              'crop'),
  'parallel' => R.read(base.merge('projection' => 'parallel', 'view_height' => 400.0)).slice(:fov, :view_height)
                 .transform_keys(&:to_s),
  'errors' => [base.merge('out' => 'out/x.png'), base.merge('width' => 0), base.reject { |k, _| k == 'fov_v_deg' },
               base.merge('projection' => 'parallel'), base.merge('style' => 'wireframe'),
               base.merge('target' => [0, -200, 60]), base.merge('up' => [0, 1, 0]),
               base.merge('expect_ir_sha256' => 'abc'), base.merge('hide_tags' => 'zone/roof')]
              .map { |r| error_of { R.read(r) } }
}
out['render_areas'] = { 'none' => R.area_tags(model, []), 'named' => R.area_tags(model, ['kitchen/island']),
                        'unknown' => error_of { R.area_tags(model, ['garage/today']) },
                        'two' => error_of { R.area_tags(model, ['kitchen/today', 'kitchen/island']) },
                        'other_top' => R.area_tags(model, [], 'TEST top') }
# The camera as SketchUp takes it: a field of view it reports across the image width is refused.
module Sketchup
  class Camera
    attr_reader :fov, :aspect_ratio

    def initialize(_eye, _target, _up, _perspective, fov = 30.0)
      @fov = fov
      @aspect_ratio = 0.0
    end

    def perspective?
      true
    end

    def fov_is_height?
      $fov_is_height
    end
  end
end

module Geom
  class Vector3d
    def initialize(*xyz)
      @xyz = xyz
    end
  end
end

class FakeView
  attr_accessor :camera

  def vpwidth
    1512
  end

  def vpheight
    848
  end
end

module HouseTwin
  module Render
    def self.check_moved(*); end
  end
end

$fov_is_height = true
taken = R.set_camera(FakeView.new, R.read(base))
$fov_is_height = false
out['fov'] = { 'height' => taken.slice('fov', 'fov_is_height'),
               'width' => error_of { R.set_camera(FakeView.new, R.read(base)) } }

# ---------------------------------------------------------------- crease intervals
out['intervals'] = {
  'intersect' => B.intersect_intervals([[0.0, 10.0], [20.0, 30.0]], [[5.0, 25.0]]),
  'subtract' => B.subtract_intervals([[0.0, 10.0]], [[2.0, 3.0], [2.5, 4.0], [9.0, 12.0]]),
  'merge' => B.merge_collinear([[[0.0, 0.0, 0.0], [5.0, 0.0, 0.0]], [[4.0, 0.0, 0.0], [9.0, 0.0, 0.0]],
                                [[0.0, 1.0, 0.0], [1.0, 1.0, 0.0]]]),
  'fmt' => [B.fmt(35.0), B.fmt(-0.0001), B.fmt(1.23456)]
}

# ---------------------------------------------------------------- creases between two solids, from face data alone
# A face as the crease pass reads it, with Face#classify_point answered from its vertices: the point dropped onto the
# plane across the normal's largest axis, on an edge within EPS, else inside or outside by the even-odd rule.
class PlainFace
  def initialize(verts, normal)
    @verts = verts
    @axis = normal.map(&:abs).each_with_index.max_by(&:first)[1]
  end

  def classify_point(pt)
    i, j = [0, 1, 2] - [@axis]
    q = [pt.x, pt.y, pt.z]
    poly = @verts.map { |v| [v[i], v[j]] }
    p2 = [q[i], q[j]]
    n = poly.length
    on = (0...n).any? do |k|
      a = poly[k]
      b = poly[(k + 1) % n]
      HouseTwin::Build.line_dist(a, b, p2) <= 1e-6 &&
        p2[0].between?([a[0], b[0]].min - 1e-6, [a[0], b[0]].max + 1e-6) &&
        p2[1].between?([a[1], b[1]].min - 1e-6, [a[1], b[1]].max + 1e-6)
    end
    return Sketchup::Face::PointOnEdge if on

    inside = false
    (0...n).each do |k|
      a = poly[k]
      b = poly[(k + 1) % n]
      next if (a[1] > p2[1]) == (b[1] > p2[1])

      inside = !inside if p2[0] < a[0] + (p2[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
    end
    inside ? Sketchup::Face::PointInside : Sketchup::Face::PointOutside
  end
end

def face_data(verts)
  nx, ny, nz = B.newell(verts)
  len = Math.sqrt(nx * nx + ny * ny + nz * nz)
  n = [nx / len, ny / len, nz / len]
  d = -(n[0] * verts[0][0] + n[1] * verts[0][1] + n[2] * verts[0][2])
  edges = verts.each_index.map { |k| [*verts[k], *verts[(k + 1) % verts.length], true] }
  B::FaceData.new(PlainFace.new(verts, n), B.box_of(verts), n, d, verts, edges)
end

# The six faces of an axis-aligned box, each turned outward.
def box_faces(x0, y0, z0, x1, y1, z1)
  [[[x0, y0, z0], [x0, y1, z0], [x1, y1, z0], [x1, y0, z0]], [[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]],
   [[x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]], [[x0, y1, z0], [x0, y1, z1], [x1, y1, z1], [x1, y1, z0]],
   [[x0, y0, z0], [x0, y0, z1], [x0, y1, z1], [x0, y1, z0]], [[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]]]
    .map { |v| face_data(v.map { |p| p.map(&:to_f) }) }
end

def leaf(id, faces)
  B::Leaf.new({ id: id, index: 0 }, nil, [], faces, B.union_box(faces.map(&:box)))
end

def creases_of(a, b)
  stats = { 'intersection_face_pairs' => 0 }
  B.crease_segments(leaf('a', a), leaf('b', b), stats).map { |p, q| [p, q].sort.map { |v| v.map { |x| x.round(6) } } }
   .sort
end

out['creases'] = {
  # a wall rising through a ceiling: one line on each of the wall's faces, none along the ceiling's own edges
  'ceiling' => creases_of(box_faces(0, 0, 0, 6, 100, 96), [face_data([[-50.0, 0.0, 90.0], [50.0, 0.0, 90.0],
                                                                       [50.0, 100.0, 90.0], [-50.0, 100.0, 90.0]])]),
  # two walls crossing: four full-height corners
  'cross' => creases_of(box_faces(0, 0, 0, 100, 6, 96), box_faces(47, -50, 0, 53, 50, 96)),
  # a wall stopping at another's face: its own end edges draw the corners, so no crease
  'tee' => creases_of(box_faces(0, 0, 0, 100, 6, 96), box_faces(47, 6, 0, 53, 50, 96)),
  # two boxes apart: nothing
  'apart' => creases_of(box_faces(0, 0, 0, 10, 10, 10), box_faces(20, 0, 0, 30, 10, 10))
}
# rubocop:enable Style/GlobalVars

puts JSON.generate(out)
