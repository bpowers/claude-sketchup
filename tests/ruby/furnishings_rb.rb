# frozen_string_literal: true
#
# bridge/furnishings.rb's plain-Ruby parts outside SketchUp, and its commands on a stand-in SketchUp model.
#
#   ruby tests/ruby/furnishings_rb.rb bridge/furnishings.rb cases.json
#
# tests/test_furnishings.py runs it when a Ruby interpreter is on the PATH. cases.json is {"recipes": [{name, recipe,
# size, finish, params}], "reads": [{name, spec}], "decide": [{name, count, recorded, entry, current}], "runs": [{name,
# tags: [[name, visible]], steps: [...]}]}. A run's steps go in order on one stand-in model: {"spec": {...}}, a
# command's spec, or {"move": id}, the owner moving that piece by hand. The stand-in holds the model's top-level
# groups, its tags and its materials; a piece's parts are drawn as empty named groups (draw_parts stood in), so a run
# tests where each piece stands, on which tag, and what each command reports.
#
# Prints one JSON object: "defaults" (RECIPES), "counts" (COUNTS as [min, max]), "recipes" (each case's parts or its
# refusal), "reads" (each spec as read, or its refusal), "decide" (what place would do), "runs" (each step's result or
# refusal, the operations run, and after the last step the model's top-level groups as a tree {name, tag, at,
# children}, its tags {name: visible} and its materials {name: [r, g, b]}).

require 'json'

load File.expand_path(ARGV.shift)
F = HouseTwin::Furnishings
cases = JSON.parse(File.read(ARGV.shift))

def attempt
  { 'ok' => yield }
rescue HouseTwin::Furnishings::Error => e
  { 'refused' => e.message }
end

# rubocop:disable Style/GlobalVars
module Geom
  # SketchUp's 4 x 4 transformation, its 16 numbers column-major as to_a gives them.
  class Transformation
    attr_reader :m

    def initialize(m = nil)
      @m = m || [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    end

    def self.translation(v)
      a = v.to_a
      new([1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, a[0], a[1], a[2], 1.0])
    end

    # about z through the origin, the only turn furnishings.rb makes
    def self.rotation(_origin, _axis, angle)
      c = Math.cos(angle)
      s = Math.sin(angle)
      new([c, s, 0.0, 0.0, -s, c, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0])
    end

    def *(other)
      a = @m
      b = other.m
      Transformation.new((0...16).map { |i| (0..3).sum { |k| a[k * 4 + i % 4] * b[(i / 4) * 4 + k] } })
    end

    def to_a
      @m.dup
    end
  end

  class Vector3d
    def initialize(*xyz)
      @xyz = xyz
    end

    def to_a
      @xyz.dup
    end
  end

  class Point3d < Vector3d; end
end

module Sketchup
  class Color
    attr_reader :rgb

    def initialize(*rgb)
      @rgb = rgb
    end
  end

  class Entity
    def initialize
      @dicts = {}
      @valid = true
    end

    def valid?
      @valid
    end

    def set_attribute(dict, key, value)
      (@dicts[dict] ||= {})[key] = value
    end

    def get_attribute(dict, key, default = nil)
      @dicts.fetch(dict, {}).fetch(key, default)
    end
  end

  class Layer < Entity
    attr_reader :name
    attr_writer :visible

    def initialize(name, visible = true)
      super()
      @name = name
      @visible = visible
    end

    def visible?
      @visible
    end
  end

  class Layers
    include Enumerable

    def initialize(tags)
      @all = [Layer.new('Layer0')] + tags.map { |name, visible| Layer.new(name, visible) }
    end

    def [](key)
      key.is_a?(Integer) ? @all[key] : @all.find { |l| l.name == key }
    end

    def add(name)
      @all << Layer.new(name)
      @all.last
    end

    def each(&block)
      @all.each(&block)
    end
  end

  class Material
    attr_reader :name
    attr_accessor :color, :alpha

    def initialize(name)
      @name = name
    end
  end

  class Materials
    def initialize(names)
      @all = names.to_h { |n| [n, Material.new(n)] }
    end

    def [](name)
      @all[name]
    end

    def add(name)
      @all[name] = Material.new(name)
    end

    def to_h
      @all.transform_values { |m| m.color&.rgb }
    end
  end

  class Drawingelement < Entity
    attr_accessor :layer

    def initialize(layer)
      super()
      @layer = layer
    end

    def hidden?
      false
    end
  end

  class Group < Drawingelement
    attr_accessor :name, :transformation
    attr_reader :entities

    def initialize(parent, layer)
      super(layer)
      @parent = parent
      @name = ''
      @entities = Entities.new
      @transformation = Geom::Transformation.new
    end

    def typename
      'Group'
    end

    def make_unique; end

    def erase!
      @parent.remove(self)
      @valid = false
    end
  end

  class ComponentInstance < Drawingelement; end

  class Entities
    def initialize
      @items = []
    end

    def add_group
      @items << Group.new(self, Sketchup.active_model.active_layer)
      @items.last
    end

    def to_a
      @items.dup
    end

    def grep(klass)
      @items.grep(klass)
    end

    def length
      @items.length
    end

    def clear!
      @items.clear
    end

    def remove(e)
      @items.delete(e)
    end
  end

  class Model
    attr_reader :entities, :layers, :materials, :operations
    attr_accessor :active_layer

    def initialize(tags, materials)
      @entities = Entities.new
      @layers = Layers.new(tags)
      @active_layer = @layers[0]
      @materials = Materials.new(materials)
      @operations = []
    end

    def active_path
      nil
    end

    def start_operation(name, *_)
      @operations << "start #{name}"
    end

    def commit_operation
      @operations << 'commit'
    end

    def abort_operation
      @operations << 'abort'
    end
  end

  def self.active_model
    $model
  end
end

# the parts drawn as empty named groups: where a piece stands is what the runs test, not its faces
module HouseTwin
  module Furnishings
    def self.draw_parts(entities, parts, _mats)
      parts.each { |q| entities.add_group.name = q['name'] }
    end
  end
end

def tree(entities)
  entities.grep(Sketchup::Group).map do |g|
    { 'name' => g.name, 'tag' => g.layer.name, 'at' => g.transformation.to_a[12, 3],
      'children' => g.name.start_with?(F::PIECE_PREFIX) ? [] : tree(g.entities) }
  end
end

def run_case(c)
  $model = Sketchup::Model.new(c['tags'] || [], c['materials'] || [])
  steps = c['steps'].map do |s|
    if s['move']
      e = F.pieces_in(F.top_group($model))[s['move']].first
      e.transformation = Geom::Transformation.translation(Geom::Vector3d.new(12.0, 0.0, 0.0)) * e.transformation
      { 'moved' => s['move'] }
    else
      attempt { F.run(JSON.generate(s['spec'])) }
    end
  end
  { 'steps' => steps, 'operations' => $model.operations, 'tree' => tree($model.entities),
    'tags' => $model.layers.to_h { |l| [l.name, l.visible?] }, 'materials' => $model.materials.to_h }
rescue StandardError => e
  { 'crash' => "#{e.class}: #{e.message}", 'backtrace' => (e.backtrace || []).first(8) }
end
# rubocop:enable Style/GlobalVars

out = {
  'defaults' => F::RECIPES,
  'counts' => F::COUNTS.transform_values { |r| [r.min, r.max] },
  'recipes' => cases.fetch('recipes', []).to_h do |c|
    [c['name'], attempt { F::Geometry.recipe(c['recipe'], c['size'], c['finish'], c['params'] || {}) }]
  end,
  'reads' => cases.fetch('reads', []).to_h do |c|
    [c['name'], attempt { F.read(JSON.generate(c['spec'])).transform_keys(&:to_s) }]
  end,
  'decide' => cases.fetch('decide', []).to_h do |c|
    [c['name'], F.decide(c['count'], c['recorded'], c['entry'], c['current']).to_s]
  end,
  'runs' => cases.fetch('runs', []).to_h { |c| [c['name'], run_case(c)] },
}
puts JSON.generate(out)
