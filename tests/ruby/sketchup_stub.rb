# frozen_string_literal: true
#
# A stand-in for the few parts of SketchUp's Ruby API that build.rb's and render.rb's bookkeeping touch (groups,
# tags, materials, attributes, operations), so those parts run under plain Ruby. No geometry: a test stubs the
# methods that draw. tests/ruby/build_cases.rb loads it before build.rb.

module Sketchup
  class Face
    PointInside = 1
    PointOnEdge = 2
    PointOnVertex = 4
    PointOutside = 16
  end

  class Edge; end

  class Color
    def initialize(r, g, b)
      @rgb = [r, g, b]
    end

    def to_a
      @rgb + [255]
    end
  end

  class AttributeDictionary
    attr_reader :name

    def initialize(name, h)
      @name = name
      @h = h
    end

    def keys
      @h.keys
    end

    def [](key)
      @h[key]
    end

    def each_pair(&block)
      @h.each_pair(&block)
    end
  end

  class Entity
    @@serial = 0 # rubocop:disable Style/ClassVars

    def initialize
      @serial = (@@serial += 1)
      @valid = true
      @dicts = {}
    end

    def entityID # rubocop:disable Naming/MethodName
      @serial
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

    def attribute_dictionary(name)
      @dicts.key?(name) ? AttributeDictionary.new(name, @dicts[name]) : nil
    end

    def attribute_dictionaries
      @dicts.empty? ? nil : @dicts.map { |name, h| AttributeDictionary.new(name, h) }
    end
  end

  class Material < Entity
    attr_reader :name, :color, :texture, :sets
    attr_accessor :alpha

    def initialize(name)
      super()
      @name = name
      @color = Color.new(0, 0, 0)
      @alpha = 1.0
      @texture = nil
      @sets = 0
    end

    def color=(c)
      @sets += 1
      @color = c
    end

    def texture=(t)
      @texture = t
    end
  end

  class Materials
    def initialize
      @all = {}
    end

    def [](name)
      @all[name]
    end

    def add(name)
      @all[name] = Material.new(name)
    end

    def length
      @all.length
    end
  end

  class LayerFolder
    attr_reader :name

    def initialize(name)
      @name = name
    end

    def visible?
      true
    end

    def count_layers
      Sketchup.active_model.layers.count { |l| l.folder == self }
    end

    def count_folders
      0
    end
  end

  class Layer < Entity
    attr_reader :name
    attr_accessor :folder
    attr_writer :visible

    def initialize(name)
      super()
      @name = name
      @visible = true
    end

    def visible?
      @visible
    end
  end

  class Layers
    include Enumerable
    attr_reader :folders

    def initialize
      @all = [Layer.new('Layer0')]
      @folders = []
    end

    def [](key)
      key.is_a?(Integer) ? @all[key] : @all.find { |l| l.name == key }
    end

    def add(name)
      @all << Layer.new(name)
      @all.last
    end

    def add_folder(name)
      @folders << LayerFolder.new(name)
      @folders.last
    end

    def remove(layer, _remove_geometry = false)
      @all.delete(layer)
    end

    def remove_folder(folder)
      @folders.delete(folder)
    end

    def each(&block)
      @all.each(&block)
    end
  end

  # A group's definition: its entities, shared by every instance of it.
  class Definition
    attr_reader :entities, :instances

    def initialize(entities)
      @entities = entities
      @instances = []
    end

    def count_instances
      @instances.count(&:valid?)
    end
  end

  class Drawingelement < Entity
    attr_accessor :layer, :material
    attr_writer :hidden, :casts_shadows, :receives_shadows

    def initialize(layer)
      super()
      @layer = layer
      @material = nil
      @hidden = false
      @casts_shadows = true
      @receives_shadows = true
    end

    def hidden?
      @hidden
    end

    def casts_shadows?
      @casts_shadows
    end

    def receives_shadows?
      @receives_shadows
    end
  end

  class Group < Drawingelement
    attr_accessor :name
    attr_writer :locked
    attr_reader :definition

    def initialize(parent, layer, definition = nil)
      super(layer)
      @parent = parent
      @name = ''
      @locked = false
      @definition = definition || Definition.new(Entities.new)
      @definition.instances << self
    end

    def entities
      @definition.entities
    end

    def locked?
      @locked
    end

    def typename
      'Group'
    end

    attr_writer :transformation

    def transformation
      @transformation || Geom::Transformation.new
    end

    def erase!
      @parent.remove(self)
      @valid = false
    end
  end

  class ComponentInstance < Drawingelement; end

  # New entities land on the model's active tag, as in SketchUp; Sketchup.created logs the tag each group got.
  class Entities
    include Enumerable

    def initialize
      @items = []
    end

    def each(&block)
      @items.dup.each(&block)
    end

    def count
      @items.length
    end

    def add_group
      layer = Sketchup.active_model.active_layer
      Sketchup.created << layer.name
      @items << Group.new(self, layer)
      @items.last
    end

    def add_instance(definition, _transformation)
      @items << Group.new(self, Sketchup.active_model.active_layer, definition)
      @items.last
    end

    def grep(klass)
      @items.grep(klass)
    end

    def remove(e)
      @items.delete(e)
    end
  end

  class Model
    attr_reader :entities, :layers, :materials, :shadow_info, :options, :operations
    attr_accessor :active_layer, :title, :path

    def initialize
      @layers = Layers.new
      @active_layer = @layers[0]
      @entities = Entities.new
      @materials = Materials.new
      @shadow_info = {}
      @options = { 'UnitsOptions' => {} }
      @operations = []
      @title = 'Untitled'
      @path = ''
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

  class << self
    attr_accessor :active_model

    def created
      @created ||= []
    end

    def version
      '26.0.0'
    end
  end
end

# UI's timer, run by hand: a test calls UI.fire to run every timer's block once.
module UI
  @timers = {}

  class << self
    def start_timer(_seconds, _repeat = false, &block)
      id = @timers.length + 1
      @timers[id] = block
      id
    end

    def stop_timer(id)
      @timers.delete(id)
    end

    def fire
      @timers.values.each(&:call)
    end
  end
end

module Geom
  # The identity, or with moved a transformation that is not (a group moved by hand).
  class Transformation
    def initialize(moved = false)
      @moved = moved
    end

    def identity?
      !@moved
    end
  end

  class Point3d
    attr_reader :x, :y, :z

    def initialize(x = 0.0, y = 0.0, z = 0.0)
      @x = x
      @y = y
      @z = z
    end
  end
end
