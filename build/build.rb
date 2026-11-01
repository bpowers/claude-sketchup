# frozen_string_literal: true
#
# build.rb - draws the house in SketchUp from the planner's plans (docs/IR.md).
#
# This file draws; it decides nothing about what a document means. ir/plan.py checks every element, fills in each
# default and resolves what the drawing needs (a wall's openings as rectangles, a unit's lights, a casing's outline),
# and writes a plan; bridge/client.py and areasc.py write plans, and this file reads them (read_plan). A document that
# does not plan is refused in Python, naming each element, and never reaches SketchUp. SketchUp's internal unit is the
# inch, as the document's is, so nothing is converted.
#
# Two entry points, both one undo step:
# - run(plan, top_name:) draws one plan whole into a top group: a fixture, a test, or `client.py build house.json`.
# - sync(manifest, rebuild:) makes HOUSE hold exactly what areasc.py's manifest places: today's house and every design
#   alternative, each piece under the tag chains it shows with, built in place (see "Built in place").
#
# Organisation
#
#   HOUSE                           top group, made on Untagged (Layer0); a tag the owner gives it stays
#     zone/<zone>                   one container per zone, tagged zone/<zone>
#       <area>/<alt>                chain containers, one nested group per tag of a piece's chain, tagged with it
#         <element id>              one group per element, tagged type/<type>
#           body, frame, glass ...  parts, some tagged part/<part>
#       <element id>                a shell piece (it shows in every combination) stands in the zone group itself
#     intersections                 creases where elements meet, tagged part/intersections
#       <id a> x <id b>             one group per pair of elements that meet
#         <tag> > <tag> > ...       one nested group per tag of both (chains, zones, types, parts), then the edges
#
# SketchUp gives an entity one tag, so the tags nest: an element shows only while every tag above it is on. Each
# element group records id, type, zone, key, src and its planned JSON in the attribute dictionary house_twin; HOUSE
# records ir, ir_sha256, manifest_sha256, build_sha256, built_at and the design areas. A unit's frame, glass and leaf
# parts record the opening's id (house_twin/opening), so a render can open a door (render.rb hide_leaves).
#
# Built in place
#
# What a build costs is the entities it adds and erases (a renderer extension watching the model, such as Enscape,
# makes each one about 20 times dearer), so sync goes on in the HOUSE the model holds:
# - a placement HOUSE holds with its piece's key (a digest of the planned element) stays;
# - one whose key another group holds is another instance of that group's definition, so a piece is drawn once however
#   many chains show it, and a piece that only moves to another chain is not drawn again;
# - only a piece nobody holds is built, from the plan the manifest names, and what the manifest no longer places is
#   erased;
# - the creases of every pair that names an element built, instanced or erased are drawn again, and no others.
# An edit to this file rebuilds everything once (HOUSE records the sha256 of the build.rb that drew it), and
# `rebuild: true` (look.py --rebuild) forces it. run always draws whole.
#
# Creases
#
# Every element is its own group and nothing is merged, so where two solids meet (an inside corner where two walls
# overlap, a ceiling running into a wall, a cabinet against a wall) SketchUp has no edge, and an edges render would miss
# the line. draw_creases computes those lines from the built faces and adds them under HOUSE/intersections, nested in
# both elements' tags so a crease shows only while both elements do. The element groups are never touched.
#
# Checks
#
# Every solid is compared with the volume its plan describes, and every wall's faces with the areas each side must
# cover, so a missing pier or an extra face fails the build rather than rendering quietly wrong. A failure raises
# BuildError naming the element and aborts the operation, leaving the previous HOUSE as it was.
#
# What a build never touches: tags the owner has already set (an area's alternative tag is created hidden and its
# today tag shown, once), the active tag (Layer0 is current while drawing and the owner's put back), the camera, the
# FURNISHINGS group and every top-level group but its own. A HOUSE build also writes out/build_report.json and sets
# the model's geo-location from the plan's location, each key only where it differs.

require 'json'
require 'fileutils'
require 'time'
require 'digest'

module HouseTwin
  # Recreated on every load, so a reload never leaves stale methods or constants behind.
  %i[Build Render BuildError].each { |c| remove_const(c) if const_defined?(c, false) }

  # A build failure. element_id names the element responsible, when there is one.
  class BuildError < StandardError
    attr_reader :element_id

    def initialize(message, element_id = nil)
      super(element_id ? "#{element_id}: #{message}" : message)
      @element_id = element_id
    end
  end

  module Build
    ROOT         = File.expand_path('..', __dir__)
    OUT          = File.join(ROOT, 'out')
    REPORT       = File.join(OUT, 'build_report.json')
    VIEWS_DIR    = File.join(OUT, 'views')
    PALETTE_PATH = File.join(__dir__, 'palette.json')
    TOP_NAME     = 'HOUSE'
    FURNISHINGS  = 'FURNISHINGS'           # the loose furniture's top group (bridge/furnishings.rb); never touched here
    DICT         = 'house_twin'
    # The sha256 of this file's bytes, recorded on HOUSE: the watcher loads this file for every command, so it is the
    # code that drew the group, and a HOUSE another build.rb drew is built again whole.
    SOURCE_SHA256 = begin
      Digest::SHA256.hexdigest(File.binread(__FILE__))
    rescue SystemCallError, IOError
      nil
    end
    PLAN_VERSION     = 1
    MANIFEST_VERSION = 1
    TODAY_PLAN       = 'today'
    TYPES      = %w[space slab wall column beam stair box roof_plane].freeze
    PART_TAGS  = %w[part/floor part/ceiling part/glass part/frame part/leaf part/trim part/intersections].freeze
    INTERSECTIONS     = 'intersections'
    INTERSECTIONS_TAG = 'part/intersections'
    NO_CREASE_TAGS    = %w[part/glass].freeze  # the edges style leaves glass out, so a crease on it would float
    CHAIN_ATTR        = 'chain'                # a chain container records its tag's name
    KEY_ATTR          = 'key'                  # an element group records its piece's content key
    PLACEHOLDER       = 'placeholder'
    GLASS             = 'glass'
    EPS               = 0.001                  # in: SketchUp merges vertices closer than this
    PARALLEL_SIN      = 1e-9                   # |n1 x n2| below this: two planes are parallel
    ROUND_SEGMENTS    = 24                     # the sides of a round column
    MENU_TITLE        = 'House twin'
    # location key in the plan => the shadow_info key it sets. Nothing else in shadow_info is ever set: the time,
    # daylight saving and whether shadows show are the owner's sun study.
    LOCATION_KEYS = { 'city' => 'City', 'country' => 'Country', 'latitude' => 'Latitude',
                      'longitude' => 'Longitude', 'tz_offset' => 'TZOffset', 'north_angle' => 'NorthAngle' }.freeze

    REQUIRED = Object.new.freeze

    # The faces of one group that may meet another element, with the tags of the group and every group above it. Face
    # data are plain floats: box [x0, y0, z0, x1, y1, z1], unit normal, plane offset d (n . p + d = 0), vertices, and
    # edges [ax, ay, az, bx, by, bz, drawn], drawn false for a soft or hidden edge, which no render shows.
    Leaf     = Struct.new(:el, :group, :tags, :faces, :box)
    FaceData = Struct.new(:face, :box, :normal, :d, :verts, :edges)
    CreaseSink = Struct.new(:top, :container, :pair_groups, :dests, :made)
    # Face#classify_point answers that put a point on a closed face.
    ON_FACE = [Sketchup::Face::PointInside, Sketchup::Face::PointOnEdge, Sketchup::Face::PointOnVertex].freeze

    # Typed reads of one JSON object (render requests). Every failure raises BuildError naming what was read and the
    # field.
    class Fields
      attr_reader :h

      def initialize(h, what)
        @h = h
        @what = what
        fail!("expected a JSON object, got #{h.inspect[0, 80]}") unless h.is_a?(Hash)
      end

      def fail!(msg)
        raise BuildError, "#{@what}: #{msg}"
      end

      def fetch(k, default, what)
        v = @h[k]
        return [false, v] unless v.nil?
        fail!("'#{k}' is missing (#{what})") if default.equal?(REQUIRED)
        [true, default]
      end

      def num(k, default = REQUIRED)
        dflt, v = fetch(k, default, 'a number')
        return v if dflt
        fail!("'#{k}' must be a finite number, got #{v.inspect}") unless v.is_a?(Numeric) && v.to_f.finite?
        v.to_f
      end

      def int(k, default = REQUIRED)
        dflt, v = fetch(k, default, 'a whole number')
        return v if dflt
        ok = v.is_a?(Integer) || (v.is_a?(Float) && v.finite? && v == v.round)
        fail!("'#{k}' must be a whole number, got #{v.inspect}") unless ok
        v.to_i
      end

      def str(k, allowed = nil, default = REQUIRED)
        dflt, v = fetch(k, default, 'a string')
        return v if dflt
        fail!("'#{k}' must be a string, got #{v.inspect}") unless v.is_a?(String)
        fail!("'#{k}' is #{v.inspect}; expected one of: #{allowed.join(' ')}") if allowed && !allowed.include?(v)
        v
      end

      def bool(k, default = REQUIRED)
        dflt, v = fetch(k, default, 'true or false')
        return v if dflt
        fail!("'#{k}' must be true or false, got #{v.inspect}") unless [true, false].include?(v)
        v
      end

      def strings(k, default = [])
        dflt, v = fetch(k, default, 'a list of strings')
        return v if dflt
        fail!("'#{k}' must be a list of strings, got #{v.inspect[0, 80]}") unless v.is_a?(Array) && v.all?(String)
        v
      end

      def nums(k, n)
        _, v = fetch(k, REQUIRED, "#{n} numbers")
        unless v.is_a?(Array) && v.length == n && v.all? { |x| x.is_a?(Numeric) && x.to_f.finite? }
          fail!("'#{k}' must be #{n} numbers, got #{v.inspect[0, 80]}")
        end
        v.map(&:to_f)
      end
    end

    # A vertical plane through a centreline a -> b: d runs along the line, off along its left normal (a -> b turned
    # +90 degrees), z is absolute.
    class LineFrame
      attr_reader :length, :ux, :uy, :nx, :ny

      def initialize(a, b)
        @ax, @ay = a
        dx = b[0] - @ax
        dy = b[1] - @ay
        @length = Math.hypot(dx, dy)
        @ux = dx / @length
        @uy = dy / @length
        @nx = -@uy
        @ny = @ux
      end

      def point(d, z, off = 0.0)
        Geom::Point3d.new(@ax + @ux * d + @nx * off, @ay + @uy * d + @ny * off, z)
      end

      def local(p)
        [(p.x.to_f - @ax) * @ux + (p.y.to_f - @ay) * @uy, p.z.to_f]
      end

      # The signed distance of p from the centreline, positive to the left.
      def offset(p)
        (p.x.to_f - @ax) * @nx + (p.y.to_f - @ay) * @ny
      end

      def normal
        Geom::Vector3d.new(@nx, @ny, 0.0)
      end

      def along
        Geom::Vector3d.new(@ux, @uy, 0.0)
      end
    end

    class << self
      # ================================================================ entry

      # Draw one plan whole into the top group top_name, every element in no chain. Returns the build stats.
      def run(plan_path, top_name: TOP_NAME)
        plan = read_plan(plan_path)
        by_id = plan[:elements].to_h { |el| [el[:id], el] }
        pieces = plan[:elements].each_with_index.map do |el, i|
          { 'id' => el[:id], 'key' => nil, 'plan' => nil, 'rank' => i, 'zone' => el[:zone], 'chains' => [[]] }
        end
        draw(plan, pieces, ->(piece) { by_id[piece['id']] }, top_name: top_name, rebuild: true)
      end

      # Make HOUSE (or top_name, for a test) hold exactly what a manifest places (areasc.py; docs/IR.md "The
      # manifest"), changing only what changed unless rebuild. Returns the build stats.
      def sync(manifest_path, rebuild: false, top_name: TOP_NAME)
        path, man = read_manifest(manifest_path)
        plans = Hash.new do |h, name|
          file = man['plans'][name] or raise BuildError, "the manifest names no plan #{name.inspect}"
          h[name] = read_plan(file)
        end
        by_id = Hash.new { |h, name| h[name] = plans[name][:elements].to_h { |el| [el[:id], el] } }
        el_of = lambda do |piece|
          by_id[piece['plan']][piece['id']] or
            raise BuildError.new("the plan #{man['plans'][piece['plan']]} holds no such element", piece['id'])
        end
        man['pieces'].each_with_index { |piece, i| piece['rank'] ||= i }
        draw(plans[TODAY_PLAN], man['pieces'], el_of, top_name: top_name, rebuild: rebuild, man: man, man_path: path)
      end

      # What run and sync share: the top group made to hold exactly `pieces` ({id, key, plan, rank, zone, chains}, the
      # planned element of each from el_of), recording `plan`, the plan of the house the group is.
      def draw(plan, pieces, el_of, top_name:, rebuild:, man: nil, man_path: nil)
        t0 = Time.now
        house = top_name == TOP_NAME
        model = Sketchup.active_model
        if model.active_path
          raise BuildError, "a group or component is open for editing in SketchUp; close it before building, " \
                            "because the build changes the #{top_name} group"
        end
        raise BuildError, "#{FURNISHINGS} is the furniture's group; a build never draws into it" if top_name == FURNISHINGS

        stats = new_stats(plan, top_name)
        stats['manifest_sha256'] = man && man['sha256']
        stats['placements'] = pieces.sum { |piece| piece['chains'].length }
        location = man ? (man['location'] || plan[:location]) : plan[:location]
        owner_tag = model.active_layer
        untagged = model.layers[0]
        creases = []
        top = nil
        model.start_operation("House twin: build #{File.basename(plan[:ir].to_s)}", true)
        begin
          model.active_layer = untagged unless owner_tag == untagged
          top = rebuild ? nil : reusable_top(model, top_name)
          stats['in_place'] = !top.nil?
          first_house = house && house_group(model).nil?
          kept = top ? nil : purge(model, top_name)
          setup_units(model) if first_house
          stats['location'] = apply_location(model, location) if house && location
          mats = phase(stats, 'materials') { ensure_materials(model, read_palette) }
          ensure_tags(model, plan[:zones] || {})
          area_tags(model, man) if man
          unless top # a new top group is made on Untagged; a tag the owner gives it stays
            top = model.entities.add_group
            top.name = top_name
            top.layer = untagged
          end
          { 'ir' => plan[:ir], 'ir_sha256' => plan[:ir_sha256], 'build_sha256' => SOURCE_SHA256,
            'manifest' => man_path, 'manifest_sha256' => man && man['sha256'], 'elements' => plan[:elements].length,
            'built_at' => Time.now.utc.iso8601(3), 'areas' => JSON.generate(man ? man['areas'] : []),
            'zones' => JSON.generate(plan[:zones] || {}) }.each { |k, v| top.set_attribute(DICT, k, v) }
          changed = phase(stats, 'elements') { place(model, top, pieces, el_of, mats, stats) }
          ranks = pieces.to_h { |piece| [[piece['id'], piece['key']], piece['rank']] }
          creases = phase(stats, 'creases') do
            draw_creases(model, top, man, ranks, stats, stats['in_place'] ? changed : nil)
          end
          prune_empty(top) if stats['in_place']
          stats['kept_state'] = keep_owner_state(top, kept) if kept
          model.active_layer = owner_tag unless model.active_layer == owner_tag
          phase(stats, 'commit') { model.commit_operation }
        rescue Exception # rubocop:disable Lint/RescueException
          model.abort_operation
          model.active_layer = owner_tag if owner_tag.valid? && model.active_layer != owner_tag
          raise
        end
        rows = []
        each_element_group(top) { |_, g, chain| rows << element_report(g, chain) }
        stats['faces'] = rows.sum { |r| r['faces'] }
        stats['seconds'] = (Time.now - t0).round(2)
        write_report(stats, rows, creases) if house
        refresh_menu(model) if house
        stats
      end

      # Make the top group hold every piece under each of its chains, and nothing else (see "Built in place"). Returns
      # the ids of the elements built, instanced or erased, as a hash.
      def place(model, top, pieces, el_of, mats, stats)
        settle(model, top, stats)
        have = Hash.new { |h, k| h[k] = [] }
        donors = {}
        each_element_group(top) do |zg, g, chain|
          id = element_id(g)
          have[[zg.name, chain, id]] << g
          key = g.get_attribute(DICT, KEY_ATTR)
          donors[[id, key]] ||= g if key && g.transformation.identity?
        end
        zones = top.entities.grep(Sketchup::Group).select { |g| g.name.start_with?('zone/') }.to_h { |g| [g.name, g] }
        containers = {}
        keep = {}
        changed = {}
        unnamed = []
        pieces.each do |piece|
          id = piece['id']
          key = piece['key']
          zname = "zone/#{piece['zone']}"
          piece['chains'].each do |chain|
            here = key && have[[zname, chain, id]].find do |g|
              !keep[g.entityID] && g.get_attribute(DICT, KEY_ATTR) == key && g.transformation.identity?
            end
            if here
              keep[here.entityID] = true
              stats['kept'] += 1
              next
            end
            donor = key && donors[[id, key]]
            el = donor ? nil : el_of.call(piece)
            next if el && draws_nothing?(el)

            zg = zones[zname] ||= zone_group(model, top, piece['zone'])
            parent = chain.empty? ? zg : chain_group(model, zg, chain, containers)
            if donor
              g = instance_of(parent, donor)
              unnamed << [g, id]
              stats['instanced'] += 1
            else
              g = build_element(model, parent, el, mats, stats)
              g.set_attribute(DICT, KEY_ATTR, key) if key
              donors[[id, key]] = g if key
              stats['built'] += 1
            end
            changed[id] = true
            keep[g.entityID] = true
          end
        end
        have.each do |(_, _, id), groups|
          groups.each do |g|
            next if keep[g.entityID] || !g.valid?

            g.erase!
            stats['erased'] += 1
            changed[id] = true
          end
        end
        # Naming a group through the API gives it a definition of its own, which is the copy an instance saves, so an
        # instance takes its element's id as its name only where no other group shows its definition.
        unnamed.each { |g, id| g.name = id if g.valid? && g.definition.count_instances == 1 }
        changed
      end

      # What the owner may have done by hand to the groups a sync goes on in, undone before it places anything. A zone
      # group or chain container moved by hand is erased, so the pieces in it are built again where they belong (an
      # element group moved by hand is never kept, see place). Every group left is put back on its tag: a tag deleted
      # in SketchUp with "move its entities to Untagged" leaves its groups untagged, so an alternative's pieces or
      # creases would show in every combination. Re-tagging adds and erases nothing, so a renderer pays nothing for it.
      def settle(model, top, stats)
        top.entities.grep(Sketchup::Group).each do |g|
          if g.name.start_with?('zone/')
            next settle_group(model, g, g.name, stats)
          elsif g.name == INTERSECTIONS
            retag(g, tag(model, INTERSECTIONS_TAG))
            # pair groups stand untagged; below them each group is named for the tag it carries (crease_dest)
            g.entities.grep(Sketchup::Group).each do |pair|
              stack = pair.entities.grep(Sketchup::Group)
              until stack.empty?
                x = stack.pop
                retag(x, tag(model, x.name))
                stack.concat(x.entities.grep(Sketchup::Group))
              end
            end
          end
        end
      end

      # A zone group or chain container: erased when moved, else re-tagged, with everything in it.
      def settle_group(model, g, tag_name, stats)
        unless g.transformation.identity?
          g.erase!
          stats['moved_containers'] += 1
          return
        end
        retag(g, tag(model, tag_name))
        g.entities.grep(Sketchup::Group).each do |x|
          chain = x.get_attribute(DICT, CHAIN_ATTR)
          type = x.get_attribute(DICT, 'type')
          if chain
            settle_group(model, x, chain, stats)
          elsif TYPES.include?(type)
            retag(x, tag(model, "type/#{type}"))
          end
        end
      end

      def retag(g, layer)
        g.layer = layer unless g.layer == layer
      end

      # A space that builds neither floor nor ceiling draws nothing, so it has no group.
      def draws_nothing?(el)
        el[:type] == 'space' && !el[:build_floor] && !el[:build_ceiling]
      end

      # Erases the chain containers and zone groups of a top group that hold nothing, deepest first.
      def prune_empty(top)
        empty = lambda do |g|
          g.entities.grep(Sketchup::Group).each { |sub| empty.call(sub) if sub.get_attribute(DICT, CHAIN_ATTR) }
          g.erase! if g.entities.count.zero?
        end
        top.entities.grep(Sketchup::Group).each { |zg| empty.call(zg) if zg.name.start_with?('zone/') }
      end

      # ================================================================ reading

      # [absolute path, parsed JSON] of a file, string keys unless symbolize.
      def read_json(path, what, symbolize: false)
        path = File.expand_path(path.to_s)
        raise BuildError, "no #{what} at #{path}" unless File.file?(path)

        [path, JSON.parse(File.binread(path).force_encoding(Encoding::UTF_8), symbolize_names: symbolize)]
      rescue JSON::ParserError => e
        raise BuildError, "#{path} is not valid JSON: #{e.message[0, 200]}"
      end

      # A plan file (ir/plan.py write_plan) with symbol keys throughout but its location's. Refuses anything else, such
      # as a house.json sent as it is, which bridge/client.py plans first.
      def read_plan(plan_path)
        path, plan = read_json(plan_path, 'plan', symbolize: true)
        unless plan.is_a?(Hash) && plan[:plan] == PLAN_VERSION && plan[:elements].is_a?(Array)
          raise BuildError, "#{path} is not a plan of version #{PLAN_VERSION}: build.rb draws planned documents " \
                            '(ir/plan.py write_plan), which bridge/client.py makes of a house.json before sending it'
        end
        plan[:location] = plan[:location]&.to_h { |k, v| [k.to_s, v] }
        plan
      end

      # The manifest areasc.py writes, as parsed JSON (string keys), checked as far as sync relies on it.
      def read_manifest(manifest_path)
        path, man = read_json(manifest_path, 'areas manifest (areasc.py writes it)')
        unless man.is_a?(Hash) && man['manifest'] == MANIFEST_VERSION
          raise BuildError, "#{path} is not a manifest of version #{MANIFEST_VERSION} (areasc.py writes one)"
        end
        %w[areas pieces].each do |k|
          raise BuildError, "#{path}: '#{k}' must be a list" unless man[k].is_a?(Array)
        end
        raise BuildError, "#{path}: 'plans' must name the plan '#{TODAY_PLAN}'" unless man['plans'].is_a?(Hash) &&
                                                                                       man['plans'][TODAY_PLAN]
        man['areas'].each do |a|
          unless a.is_a?(Hash) && a['id'].is_a?(String) && a['tags'].is_a?(Array) && a['tags'].include?(a['today'])
            raise BuildError, "#{path}: an area must be {id, name, tags, today} with today among its tags, got " \
                              "#{a.inspect[0, 120]}"
          end
        end
        man['pieces'].each do |p|
          next if p.is_a?(Hash) && p['id'].is_a?(String) && p['zone'].is_a?(String) && p['chains'].is_a?(Array) &&
                  p['chains'].all? { |c| c.is_a?(Array) && c.all?(String) } && p['plan'].is_a?(String)

          raise BuildError, "#{path}: a piece must be {id, key, plan, zone, chains}, got #{p.inspect[0, 120]}"
        end
        [path, man]
      end

      # build/palette.json: {key => {"rgb" => [r, g, b], "alpha" => a}}, checked.
      def read_palette(path = PALETTE_PATH)
        path, pal = read_json(path, 'palette')
        raise BuildError, "#{path} must be an object of finishes" unless pal.is_a?(Hash) && pal.key?(PLACEHOLDER)

        pal.each do |name, spec|
          rgb = spec.is_a?(Hash) && spec['rgb']
          unless rgb.is_a?(Array) && rgb.length == 3 && rgb.all? { |c| c.is_a?(Integer) && c.between?(0, 255) }
            raise BuildError, "#{path}: #{name} needs \"rgb\": [r, g, b], each 0..255, got #{spec.inspect[0, 80]}"
          end
          alpha = spec.fetch('alpha', 1.0)
          raise BuildError, "#{path}: #{name}'s alpha must lie in 0..1" unless alpha.is_a?(Numeric) && alpha.between?(0, 1)
        end
        pal
      end

      # ================================================================ identity

      def house_group(model = Sketchup.active_model, top_name = TOP_NAME)
        model.entities.grep(Sketchup::Group).find { |g| g.valid? && g.name == top_name }
      end

      # What SketchUp holds: {ir, ir_sha256, manifest_sha256, build_sha256, built_at, elements, top} from the top
      # group's attributes, or nil when there is none.
      def identity(model = Sketchup.active_model, top_name = TOP_NAME)
        top = house_group(model, top_name)
        return nil unless top

        out = %w[ir ir_sha256 manifest_sha256 build_sha256 built_at elements].to_h { |k| [k, top.get_attribute(DICT, k)] }
        out.merge('top' => top_name)
      end

      # The element group for an id under HOUSE (the first, where chains show it more than once), or nil.
      def find(id, model = Sketchup.active_model)
        top = house_group(model)
        return nil unless top

        each_element_group(top) { |_, g, _| return g if element_id(g) == id }
        nil
      end

      # An element group's id: its record, which an instance carries where it has no name of its own (instance_of).
      def element_id(g)
        g.get_attribute(DICT, 'id') || g.name
      end

      # The top group a build can go on in: the one of that name, drawn by this build.rb, where it was built.
      def reusable_top(model, top_name)
        tops = model.entities.grep(Sketchup::Group).select { |g| g.valid? && g.name == top_name }
        return nil unless tops.length == 1 && SOURCE_SHA256

        top = tops.first
        top.get_attribute(DICT, 'build_sha256') == SOURCE_SHA256 && top.transformation.identity? ? top : nil
      end

      # ================================================================ design areas
      #
      # Each alternative is a tag <area>/<alt> in a tag folder named for its area, today's among them. A chain is a
      # list of such tags, one per area; its container nests one group per tag, so what stands in it shows only while
      # every tag of the chain is on. With every area on its today tag, HOUSE shows today's house.

      # The chain container for `chain` inside zone group `parent`, nested one group per tag and made on first use.
      def chain_group(model, parent, chain, cache)
        chain.each_with_index.reduce(parent) do |into, (name, i)|
          cache[[parent, chain[0..i]]] ||= begin
            g = into.entities.grep(Sketchup::Group).find { |x| x.get_attribute(DICT, CHAIN_ATTR) == name }
            unless g
              g = into.entities.add_group
              g.name = name
              g.layer = tag(model, name)
              g.set_attribute(DICT, CHAIN_ATTR, name)
            end
            g
          end
        end
      end

      # Yields [zone group, element group, the tags of its chain] for every element group of a top group.
      def each_element_group(top)
        top.entities.grep(Sketchup::Group).each do |zg|
          next unless zg.valid? && zg.name.start_with?('zone/')

          stack = [[zg, []]]
          until stack.empty?
            into, chain = stack.pop
            into.entities.grep(Sketchup::Group).each do |g|
              name = g.get_attribute(DICT, CHAIN_ATTR)
              name ? stack << [g, chain + [name]] : yield(zg, g, chain)
            end
          end
        end
      end

      # The areas' tags, each in its area's folder: {name => tag}. A tag new to the model is shown when it is its
      # area's today and hidden otherwise, so a first sync changes nothing the owner sees; a tag the model already has
      # keeps the visibility the owner gave it.
      def area_tags(model, man)
        man['areas'].each_with_object({}) do |a, out|
          a['tags'].each do |name|
            fresh = model.layers[name].nil?
            layer = tag(model, name)
            layer.visible = (name == a['today']) if fresh
            out[name] = layer
          end
        end
      end

      # The design areas HOUSE was last synced with: [{"id", "name", "tags", "today"}], [] for none.
      def area_list(model = Sketchup.active_model)
        top = house_group(model)
        raw = top && top.get_attribute(DICT, 'areas')
        list = raw ? JSON.parse(raw) : []
        list.is_a?(Array) ? list : []
      rescue JSON::ParserError
        []
      end

      # Another instance of an element group's definition in `parent`, with the group's tag, material and record: the
      # same geometry under another chain. It takes no name (see place).
      def instance_of(parent, donor)
        g = parent.entities.add_instance(donor.definition, Geom::Transformation.new)
        g.layer = donor.layer
        g.material = donor.material
        donor.attribute_dictionary(DICT)&.each_pair { |k, v| g.set_attribute(DICT, k, v) }
        g
      end

      # Whether two placements can show at once: no area named in both with different tags. `a` and `b` map an area
      # id to the tag a chain holds for it.
      def chains_agree?(a, b)
        a.all? { |area, name| !b.key?(area) || b[area] == name }
      end

      # The element groups showing while exactly the area tags `on` are on: {id => group}.
      def shown_elements(model, on, top_name = TOP_NAME)
        out = {}
        top = house_group(model, top_name)
        each_element_group(top) { |_, g, chain| out[element_id(g)] ||= g if (chain - on).empty? } if top
        out
      end

      # Switch design areas so each shows one alternative: `choice` maps an area id to an alternative ('island') or
      # its tag ('kitchen/island'); an area it does not name keeps the alternative showing now (or its today, where
      # none or several show). One undo step. Returns {area id => the tag now on}.
      def choose(choice, model = Sketchup.active_model)
        areas = area_list(model)
        raise BuildError, 'this model has no design areas; sync them first (look.py)' if areas.empty?

        ids = areas.map { |a| a['id'] }
        unknown = choice.keys - ids
        raise BuildError, "no design area #{unknown.join(', ')} (the areas are #{ids.join(', ')})" unless unknown.empty?

        on = areas.to_h do |a|
          want = choice[a['id']]
          if want
            name = want.include?('/') ? want : "#{a['id']}/#{want}"
            unless a['tags'].include?(name)
              raise BuildError, "#{a['id']} has no alternative #{want} (it has #{a['tags'].join(', ')})"
            end
          else
            shown = a['tags'].select { |t| model.layers[t]&.visible? }
            name = shown.length == 1 ? shown.first : a['today']
          end
          [a['id'], name]
        end
        model.start_operation('House twin: choose design areas', true)
        begin
          areas.each do |a|
            a['tags'].each do |t|
              layer = model.layers[t]
              layer.visible = (t == on[a['id']]) if layer && layer.visible? != (t == on[a['id']])
            end
          end
          model.commit_operation
        rescue Exception # rubocop:disable Lint/RescueException
          model.abort_operation
          raise
        end
        on
      end

      # The owner's switch: Extensions > House twin > <area> > <alternative>, a check mark on the one showing. SketchUp
      # cannot take a menu item back, so items are only ever added: one per area and alternative the model's HOUSE has
      # been synced with in this session. The items call whatever build.rb is loaded when clicked.
      def refresh_menu(model = Sketchup.active_model)
        return unless defined?(UI) && UI.respond_to?(:menu) && model

        areas = area_list(model)
        state = HouseTwin.menu_state
        return if areas.empty?

        state[:root] ||= UI.menu('Extensions').add_submenu(MENU_TITLE)
        areas.each do |a|
          sub = state[:areas][a['id']] ||= state[:root].add_submenu(a['name'] || a['id'])
          a['tags'].each do |t|
            next if state[:items][[a['id'], t]]

            area = a['id']
            item = sub.add_item(t.split('/', 2)[1]) { HouseTwin::Build.choose_from_menu(area, t) }
            sub.set_validation_proc(item) { HouseTwin::Build.menu_state_of(area, t) } if item
            state[:items][[area, t]] = true
          end
        end
      end

      def choose_from_menu(area, tag_name)
        choose({ area => tag_name })
      rescue BuildError => e
        UI.messagebox("The design areas were not switched.\n\n#{e.message}")  # a menu block's raise is seen by nobody
      end

      def menu_state_of(area, tag_name)
        model = Sketchup.active_model
        return MF_GRAYED unless area_list(model).any? { |a| a['id'] == area && a['tags'].include?(tag_name) }

        model.layers[tag_name]&.visible? ? MF_CHECKED : MF_UNCHECKED
      end

      # ================================================================ model state

      # Runs one phase of a build and adds its seconds to stats['phases'].
      def phase(stats, name)
        t = Time.now
        out = yield
        stats['phases'][name] = ((stats['phases'][name] || 0.0) + (Time.now - t)).round(3)
        out
      end

      # Erases every top-level group named name. Returns what the owner set on them that keep_owner_state puts on the
      # group built in their place, or nil when there was none: hidden when every one was, locked when any was, and
      # the first one's tag, shadow flags, material and attribute dictionaries other than DICT.
      def purge(model, name)
        old = model.entities.grep(Sketchup::Group).select { |g| g.valid? && g.name == name }
        return nil if old.empty?

        first = old.first
        dicts = (first.attribute_dictionaries || []).reject { |d| d.name == DICT }
        was = { hidden: old.all?(&:hidden?), locked: old.any?(&:locked?), layer: first.layer,
                casts_shadows: first.casts_shadows?,
                receives_shadows: first.receives_shadows?, material: first.material,
                dictionaries: dicts.to_h { |d| [d.name, d.keys.to_h { |k| [k, d[k]] }] } }
        old.each(&:erase!)
        was
      end

      def keep_owner_state(g, was)
        g.layer = was[:layer] if was[:layer]&.valid?
        g.hidden = was[:hidden]
        g.locked = was[:locked]
        g.casts_shadows = was[:casts_shadows]
        g.receives_shadows = was[:receives_shadows]
        g.material = was[:material] if g.material.nil? && was[:material]&.valid?
        was[:dictionaries].each { |d, pairs| pairs.each { |k, v| g.set_attribute(d, k, v) } }
        { 'hidden' => g.hidden?, 'locked' => g.locked?, 'tag' => g.layer.name,
          'dictionaries' => was[:dictionaries].keys }
      end

      # Inches, shown architecturally, set once when a model gets its first HOUSE; the owner may change them after.
      def setup_units(model)
        o = model.options['UnitsOptions']
        o['LengthUnit'] = 0
        o['LengthFormat'] = 1
      end

      # One material per palette key: colour and alpha set where they differ, a texture dropped.
      def ensure_materials(model, palette)
        palette.each_with_object({}) do |(name, spec), out|
          m = model.materials[name] || model.materials.add(name)
          rgb = spec['rgb']
          alpha = spec.fetch('alpha', 1.0).to_f
          m.texture = nil if m.texture
          m.color = Sketchup::Color.new(*rgb) unless m.color.to_a[0, 3] == rgb
          m.alpha = alpha unless (m.alpha.to_f - alpha).abs < 1e-6
          out[name] = m
        end
      end

      def mat(mats, key)
        mats[key] || mats[PLACEHOLDER]
      end

      # The whole tag vocabulary exists after every build, so a render may hide any declared zone's tag while a
      # misspelt tag still fails the render.
      def ensure_tags(model, zones)
        zones.each_key { |z| tag(model, "zone/#{z}") }
        TYPES.each { |t| tag(model, "type/#{t}") }
        PART_TAGS.each { |p| tag(model, p) }
      end

      # A tag, filed in a tag folder named after its prefix where SketchUp has folders.
      def tag(model, name)
        layers = model.layers
        layer = layers[name] || layers.add(name)
        if name.include?('/') && layers.respond_to?(:folders) && layer.respond_to?(:folder=)
          prefix = name.split('/').first
          folder = layers.folders.find { |x| x.name == prefix } || layers.add_folder(prefix)
          layer.folder = folder unless layer.folder == folder
        end
        layer
      end

      def zone_group(model, top, zone)
        g = top.entities.add_group
        g.name = "zone/#{zone}"
        g.layer = tag(model, "zone/#{zone}")
        g
      end

      def part(model, parent, name, tag_name = nil, material = nil)
        g = parent.entities.add_group
        g.name = name
        g.layer = tag(model, tag_name) if tag_name
        g.material = material if material
        g
      end

      # ================================================================ elements

      def build_element(model, parent, el, mats, stats)
        g = parent.entities.add_group
        g.name = el[:id]
        g.layer = tag(model, "type/#{el[:type]}")
        g.material = mat(mats, el[:finish]) if el[:finish]
        %i[id type zone src].each { |k| g.set_attribute(DICT, k.to_s, el[k].to_s) }
        g.set_attribute(DICT, 'json', JSON.generate(el))
        send("build_#{el[:type]}", model, g, el, mats, stats)
        g
      rescue BuildError
        raise
      rescue StandardError => e
        raise BuildError.new("building the #{el[:type]} failed: #{e.class}: #{e.message}", el[:id])
          .tap { |x| x.set_backtrace(e.backtrace) }
      end

      # A space's floor and ceiling are faces whose outline edges are hidden: an outline is a room's boundary, not a
      # thing, so two spaces with no wall between them draw no line, and where a floor meets a wall the crease pass
      # draws the junction (it counts hidden edges as not drawn).
      def build_space(model, g, el, mats, stats)
        if el[:build_floor]
          sub = part(model, g, 'floor', 'part/floor')
          face = face!(sub.entities, el[:outline].map { |x, y| Geom::Point3d.new(x, y, el[:floor_z]) }, 'the floor')
          face.reverse! if face.normal.z.to_f.negative?
          paint(face, mat(mats, el[:floor_finish]))
          hide_outline(sub.entities, stats)
          stats['floor_faces'] += 1
        end
        return unless el[:build_ceiling]

        sub = part(model, g, 'ceiling', 'part/ceiling')
        pts = el[:outline].map { |x, y| Geom::Point3d.new(x, y, ceiling_z(el, x, y)) }
        face = face!(sub.entities, pts, 'the ceiling')
        face.reverse! if face.normal.z.to_f.positive?
        paint(face, mat(mats, el[:ceiling_finish]))
        hide_outline(sub.entities, stats)
        stats['ceiling_faces'] += 1
        stats['sloped_ceilings'] += 1 if el[:ceiling_plane]
      end

      def ceiling_z(el, x, y)
        plane = el[:ceiling_plane]
        return el[:ceiling_z] unless plane

        ax, ay, az = plane[:at]
        sx, sy = plane[:slope]
        az + sx * (x - ax) + sy * (y - ay)
      end

      def hide_outline(ents, stats)
        ents.grep(Sketchup::Edge).each do |e|
          e.hidden = true
          stats['space_edges_hidden'] += 1
        end
      end

      def build_slab(_model, g, el, _mats, stats)
        base = el[:outline].map { |x, y| Geom::Point3d.new(x, y, el[:z_top]) }
        add_prism(g.entities, base, Geom::Vector3d.new(0, 0, -el[:thickness]))
        check_volume(g, area2(el[:outline]).abs * el[:thickness], el, 'the slab', stats)
      end

      # A wall is its elevation profile with each opening's rectangle cut out, pushed through its thickness: one solid
      # with true reveals and no seam lines across its faces, which matters for edge overlays against photographs.
      def build_wall(model, g, el, mats, stats)
        lf = LineFrame.new(el[:a], el[:b])
        t = el[:thickness]
        len = el[:length]
        holes = el[:openings].map { |o| o[:rect] }
        area = len * ((el[:top_a] - el[:z0]) + (el[:top_b] - el[:z0])) / 2.0 -
               holes.sum { |d0, d1, z0, z1| (d1 - d0) * (z1 - z0) }
        if area > EPS
          body = part(model, g, 'body')
          outer = [[0.0, el[:z0]], [len, el[:z0]], [len, el[:top_b]], [0.0, el[:top_a]]]
          add_profile_solid(body.entities, lf, outer, holes, -t / 2.0, t)
          check_volume(body, area * t, el, 'the wall body', stats)
          paint_wall_faces(body, el, lf, area, mats, stats)
        end
        el[:openings].each { |o| build_opening(model, g, el, lf, o, mats, stats) }
      end

      # Every face of a wall body is painted: one lying in the plane thickness/2 to the left of a -> b takes
      # finish_left, one in the plane to the right finish_right, and the rest (ends, reveals, top, base) finish. A
      # face's side comes from where its vertices lie, and each side must cover the area the plan describes (the
      # elevation less the openings; each end, the thickness times its height less any opening running to it), so a
      # face that came out split, missing or off its plane cannot keep the wrong finish unnoticed.
      def paint_wall_faces(body, el, lf, area, mats, stats)
        t = el[:thickness]
        t2 = t / 2.0
        len = el[:length]
        rects = el[:openings].map { |o| o[:rect] }
        expected = {
          left: area, right: area,
          end_a: t * ((el[:top_a] - el[:z0]) - rects.select { |r| r[0] <= EPS }.sum { |r| r[3] - r[2] }),
          end_b: t * ((el[:top_b] - el[:z0]) - rects.select { |r| r[1] >= len - EPS }.sum { |r| r[3] - r[2] })
        }
        covered = expected.transform_values { 0.0 }
        body.entities.grep(Sketchup::Face).each do |face|
          pts = face.vertices.map(&:position)
          offs = pts.map { |p| lf.offset(p) }
          ds = pts.map { |p| lf.local(p)[0] }
          side = if offs.all? { |o| (o - t2).abs <= EPS } then :left
                 elsif offs.all? { |o| (o + t2).abs <= EPS } then :right
                 elsif ds.all? { |d| d.abs <= EPS } then :end_a
                 elsif ds.all? { |d| (d - len).abs <= EPS } then :end_b
                 end
          key = { left: el[:finish_left], right: el[:finish_right] }.fetch(side, el[:finish])
          paint(face, mat(mats, key))
          covered[side] += face.area.to_f if side
          stats['wall_faces_painted'] += 1
        end
        covered.each do |side, got|
          want = expected[side]
          next if (got - want).abs <= [want.abs * 1e-4, 1e-3].max

          raise BuildError.new("the #{side.to_s.tr('_', ' ')} faces of the wall body cover #{fmt(got)} sq in where " \
                               "the plan describes #{fmt(want)} sq in", el[:id])
        end
      end

      # An opening's unit: one frame solid (the frame, mullions and rails) `depth` deep about its plane, a glass pane
      # per light where glazed, or a leaf panel per light for an unglazed door or window; then its casing.
      def build_opening(model, g, el, lf, o, mats, stats)
        stats['openings'] += 1
        u = o[:unit]
        if u
          d0, d1, z0, z1 = o[:rect]
          fd = u[:depth]
          outer = [[d0, z0], [d1, z0], [d1, z1], [d0, z1]]
          frame_area = (d1 - d0) * (z1 - z0) - u[:lights].sum { |a, b, c, d| (b - a) * (d - c) }
          if frame_area > EPS
            fr = part(model, g, "#{o[:id]} frame", 'part/frame', mat(mats, u[:finish]))
            fr.set_attribute(DICT, 'opening', o[:id])
            add_profile_solid(fr.entities, lf, outer, u[:lights], u[:offset] - fd / 2.0, fd)
            check_volume(fr, frame_area * fd, el, "the frame of #{o[:id]}", stats)
            stats['unit_frames'] += 1
          end
          stats['mullions'] += u[:verticals].length
          stats['rails'] += u[:horizontals].length
          if u[:glazed]
            gl = part(model, g, "#{o[:id]} glass", 'part/glass', mat(mats, GLASS))
            gl.set_attribute(DICT, 'opening', o[:id])
            u[:lights].each do |a, b, c, d|
              paint(face!(gl.entities, rect(lf, a, b, c, d, u[:offset]), "a glass pane of #{o[:id]}"), mat(mats, GLASS))
            end
            stats['glass_panes'] += u[:lights].length
          elsif %w[door window].include?(o[:kind])
            lp = part(model, g, "#{o[:id]} leaf", 'part/leaf', mat(mats, u[:leaf_finish]))
            lp.set_attribute(DICT, 'opening', o[:id])
            u[:lights].each do |a, b, c, d|
              paint(face!(lp.entities, rect(lf, a, b, c, d, u[:offset]), "a leaf panel of #{o[:id]}"),
                    mat(mats, u[:leaf_finish]))
            end
            stats['leaf_panels'] += u[:lights].length
          end
        end
        build_casing(model, g, el, lf, o, mats, stats) if o[:casing]
      end

      # A casing: on each face it names, the ring between its outer rectangle and the opening, `proud` deep off that
      # face; "casing +" on the left face, "casing -" on the right. A side of no width leaves the ring open there.
      def build_casing(model, g, el, lf, o, mats, stats)
        c = o[:casing]
        d0, d1, z0, z1 = c[:outer]
        outer = [[d0, z0], [d1, z0], [d1, z1], [d0, z1]]
        h0, h1, hz0, hz1 = o[:rect]
        area = (d1 - d0) * (z1 - z0) - (h1 - h0) * (hz1 - hz0)
        t2 = el[:thickness] / 2.0
        p = c[:proud]
        [['left', '+', t2], ['right', '-', -t2 - p]].each do |face, sign, off0|
          next unless c[:faces].include?(face)

          tg = part(model, g, "#{o[:id]} casing #{sign}", 'part/trim', mat(mats, c[:finish]))
          add_profile_solid(tg.entities, lf, outer, [o[:rect]], off0, p)
          check_volume(tg, area * p, el, "the #{face} casing of #{o[:id]}", stats)
        end
        stats['casings'] += 1
      end

      def build_column(model, g, el, _mats, stats)
        cap = el[:capital]
        shaft_top = cap ? el[:z1] - cap[:height] : el[:z1]
        shaft = part(model, g, 'shaft')
        a = column_prism(shaft.entities, el[:center], el[:shape] == 'round' ? el[:diameter] : el[:size], el[:z0],
                         shaft_top)
        check_volume(shaft, a * (shaft_top - el[:z0]), el, 'the column shaft', stats)
        return unless cap

        cg = part(model, g, 'capital')
        a = column_prism(cg.entities, el[:center], cap[:size], shaft_top, el[:z1])
        check_volume(cg, a * cap[:height], el, 'the capital', stats)
      end

      # A column's section swept from z0 to z1: `section` a diameter (a ROUND_SEGMENTS-gon, its sides soft and smooth
      # so it reads round and draws only its outline) or [w, d]. Returns the section's area.
      def column_prism(ents, center, section, z0, z1)
        cx, cy = center
        round = section.is_a?(Numeric)
        pts = if round
                r = section / 2.0
                (0...ROUND_SEGMENTS).map do |i|
                  t = (i + 0.5) * 2.0 * Math::PI / ROUND_SEGMENTS
                  [cx + r * Math.cos(t), cy + r * Math.sin(t)]
                end
              else
                hx, hy = section.map { |v| v / 2.0 }
                [[cx - hx, cy - hy], [cx + hx, cy - hy], [cx + hx, cy + hy], [cx - hx, cy + hy]]
              end
        add_prism(ents, pts.map { |x, y| Geom::Point3d.new(x, y, z0) }, Geom::Vector3d.new(0, 0, z1 - z0),
                  smooth_sides: round)
        area2(pts).abs
      end

      def build_beam(_model, g, el, _mats, stats)
        lf = LineFrame.new(el[:a], el[:b])
        w2 = el[:width] / 2.0
        z = el[:soffit_z]
        base = [lf.point(0.0, z, -w2), lf.point(lf.length, z, -w2), lf.point(lf.length, z, w2), lf.point(0.0, z, w2)]
        add_prism(g.entities, base, Geom::Vector3d.new(0, 0, el[:depth]))
        check_volume(g, el[:length] * el[:width] * el[:depth], el, 'the beam', stats)
      end

      # One solid block under each tread, built as a single sawtooth prism so no seams show between them, and the top
      # riser (from the upper floor down to tread 1, under no tread) as a face on the nosing line.
      def build_stair(model, g, el, mats, stats)
        lf = LineFrame.new(el[:a], el[:b])
        dx, dy = el[:descend]
        r = el[:risers]
        rise = el[:rise]
        run = el[:run]
        zt = el[:z_top]
        zl = zt - r * rise
        at = lambda do |w, d, z|
          p = lf.point(w, z)
          Geom::Point3d.new(p.x.to_f + dx * d, p.y.to_f + dy * d, z)
        end
        if r >= 2
          prof = [[0.0, zl], [0.0, zt - rise]]
          (1..(r - 1)).each do |k|
            prof << [k * run, zt - k * rise]
            prof << [k * run, zt - (k + 1) * rise] if k < r - 1
          end
          prof << [(r - 1) * run, zl]
          body = part(model, g, 'body')
          add_prism(body.entities, prof.map { |d, z| at.call(0.0, d, z) }, lf.along.tap { |v| v.length = lf.length })
          check_volume(body, run * rise * r * (r - 1) / 2.0 * lf.length, el, 'the stair body', stats)
          stats['treads'] += r - 1
        end
        riser = part(model, g, 'riser.top')
        face = face!(riser.entities, [at.call(0.0, 0.0, zt - rise), at.call(lf.length, 0.0, zt - rise),
                                      at.call(lf.length, 0.0, zt), at.call(0.0, 0.0, zt)], 'the top riser')
        face.reverse! if face.normal.dot(Geom::Vector3d.new(dx, dy, 0)).negative?
        paint(face, mat(mats, el[:finish]))
      end

      def build_box(_model, g, el, _mats, stats)
        x0, y0, z0, x1, y1, z1 = el[:bbox]
        base = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map { |x, y| Geom::Point3d.new(x, y, z0) }
        add_prism(g.entities, base, Geom::Vector3d.new(0, 0, z1 - z0))
        draw_front_lines(g.entities, el, stats) if el[:front] && !(el[:seams].empty? && el[:rails].empty?)
        check_volume(g, (x1 - x0) * (y1 - y0) * (z1 - z0), el, 'the box', stats)
      end

      # Seams and rails, the door and drawer lines on a box's front, drawn as segments meeting at their crossings (so
      # SketchUp never intersects crossing edges); the front must then hold exactly the panels asked for.
      def draw_front_lines(ents, el, stats)
        x0, y0, z0, x1, y1, z1 = el[:bbox]
        sign = el[:front][0] == '+' ? 1.0 : -1.0
        if el[:front][1] == 'y'
          c = sign.positive? ? y1 : y0
          lo, hi = x0, x1
          pt = ->(s, z) { Geom::Point3d.new(s, c, z) }
          axis = Geom::Vector3d.new(0, sign, 0)
          coord = ->(p) { p.y.to_f }
        else
          c = sign.positive? ? x1 : x0
          lo, hi = y0, y1
          pt = ->(s, z) { Geom::Point3d.new(c, s, z) }
          axis = Geom::Vector3d.new(sign, 0, 0)
          coord = ->(p) { p.x.to_f }
        end
        cuts = [lo] + el[:seams].map { |s| lo + s } + [hi]
        levels = [z0] + el[:rails] + [z1]
        el[:seams].each { |s| levels.each_cons(2) { |za, zb| ents.add_line(pt.call(lo + s, za), pt.call(lo + s, zb)) } }
        el[:rails].each { |z| cuts.each_cons(2) { |sa, sb| ents.add_line(pt.call(sa, z), pt.call(sb, z)) } }
        panels = ents.grep(Sketchup::Face).count do |f|
          f.normal.dot(axis) > 0.999 && f.vertices.all? { |v| (coord.call(v.position) - c).abs <= EPS }
        end
        expected = (el[:seams].length + 1) * (el[:rails].length + 1)
        raise "the front divided into #{panels} panels, expected #{expected}" unless panels == expected

        stats['box_seams'] += el[:seams].length
        stats['box_rails'] += el[:rails].length
      end

      # A roof plane is its top polygon swept straight down by its thickness, with each hole cut through.
      def build_roof_plane(_model, g, el, _mats, stats)
        prism = area2(el[:pts].map { |p| p[0, 2] }).abs * el[:thickness]
        holes = el[:holes].sum(0.0) { |x0, y0, x1, y1| (x1 - x0) * (y1 - y0) * el[:thickness] }
        add_prism(g.entities, el[:pts].map { |x, y, z| Geom::Point3d.new(x, y, z) },
                  Geom::Vector3d.new(0, 0, -el[:thickness]))
        cut_holes(g.entities, el)
        check_volume(g, prism - holes, el, 'the roof plane', stats)
        stats['roof_holes'] += el[:holes].length
      end

      # Each hole [x0, y0, x1, y1] cut straight down: its outline drawn on the top face and on the underside a
      # thickness below, the faces inside erased, and a vertical face under each side, turned into the hole.
      def cut_holes(ents, el)
        return if el[:holes].empty?

        # a vertical side's normal carries float noise in z, and the planner refuses a plane steeper than nz 0.02
        top = ents.grep(Sketchup::Face).select { |fc| fc.normal.z.to_f > 0.01 }
        raise "the roof plane has #{top.length} faces turned up, not one top face" unless top.length == 1

        a, b, c, d = top[0].plane.map(&:to_f)
        t = el[:thickness]
        el[:holes].each_with_index do |(x0, y0, x1, y1), k|
          plan = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
          upper = plan.map { |x, y| Geom::Point3d.new(x, y, -(a * x + b * y + d) / c) }
          lower = upper.map { |p| Geom::Point3d.new(p.x, p.y, p.z.to_f - t) }
          [upper, lower].each { |outline| face!(ents, outline, "the outline of holes[#{k}]").erase! }
          cx = (x0 + x1) / 2.0
          cy = (y0 + y1) / 2.0
          4.times do |i|
            j = (i + 1) % 4
            side = face!(ents, [upper[i], upper[j], lower[j], lower[i]], "a side of holes[#{k}]")
            into = Geom::Vector3d.new(cx - (plan[i][0] + plan[j][0]) / 2.0, cy - (plan[i][1] + plan[j][1]) / 2.0, 0.0)
            side.reverse! if side.normal.dot(into).negative?
          end
        end
      end

      # The plan's location into SketchUp's shadow settings, each key only where it differs. Returns the settings
      # it covers as SketchUp then holds them.
      def apply_location(model, loc)
        si = model.shadow_info
        loc.each_with_object({}) do |(k, v), out|
          sk = LOCATION_KEYS[k] or next
          si[sk] = v unless si[sk] == v
          out[sk] = si[sk]
        end
      end

      # ================================================================ primitives

      def face!(ents, pts, what)
        face = ents.add_face(pts)
        raise "SketchUp made no face for #{what}" unless face.is_a?(Sketchup::Face)

        face
      rescue ArgumentError => e
        raise "SketchUp rejected #{what}: #{e.message}"
      end

      def paint(face, material)
        face.material = material
        face.back_material = material
      end

      def rect(lf, d0, d1, z0, z1, off)
        [lf.point(d0, z0, off), lf.point(d1, z0, off), lf.point(d1, z1, off), lf.point(d0, z1, off)]
      end

      # A closed prism: a planar base polygon swept by vec, each face made explicitly and turned outward from the
      # geometry, so nothing depends on SketchUp's guesses about orientation.
      def add_prism(ents, base, vec, smooth_sides: false)
        nx, ny, nz = newell(base.map { |p| [p.x.to_f, p.y.to_f, p.z.to_f] })
        m = Geom::Vector3d.new(nx, ny, nz)
        raise 'the base polygon is degenerate' if m.length.to_f < 1e-9

        m.normalize!
        v = vec.clone
        v.normalize!
        raise 'the sweep runs parallel to the base polygon' if m.dot(v).abs < 1e-6

        top = base.map { |p| p.offset(vec) }
        cap = face!(ents, base, 'the base cap')
        cap.reverse! if cap.normal.dot(vec).positive?
        cap = face!(ents, top, 'the top cap')
        cap.reverse! if cap.normal.dot(vec).negative?
        n = base.length
        n.times do |i|
          j = (i + 1) % n
          side = face!(ents, [base[i], base[j], top[j], top[i]], "side #{i}")
          outward = (base[j] - base[i]).cross(m)
          side.reverse! if side.normal.dot(outward).negative?
        end
        return unless smooth_sides

        ents.grep(Sketchup::Edge).each do |e|
          next unless e.line[1].parallel?(vec)

          e.soft = true
          e.smooth = true
        end
      end

      # A solid through a vertical plane: the elevation profile `outer` [[d, z], ...] with rectangular holes
      # [d0, d1, z0, z1] cut through, lying at offset off0 and pushed `depth` along the plane's left normal. The
      # classic SketchUp way: draw the profile as one face, draw each hole into it, erase the holes, push the rest.
      def add_profile_solid(ents, lf, outer, holes, off0, depth)
        face!(ents, outer.map { |d, z| lf.point(d, z, off0) }, 'the elevation profile')
        holes.each do |d0, d1, z0, z1|
          ents.add_face(rect(lf, d0, d1, z0, z1, off0))
        rescue ArgumentError
          nil # the hole coincides with a face that already exists; it is found below
        end
        holes.each do |d0, d1, z0, z1|
          area = (d1 - d0) * (z1 - z0)
          ents.grep(Sketchup::Face).each do |f|
            next unless f.valid? && (f.area.to_f - area).abs <= 1e-6 * [area, 1.0].max

            inside = f.vertices.all? do |vx|
              d, z = lf.local(vx.position)
              d >= d0 - EPS && d <= d1 + EPS && z >= z0 - EPS && z <= z1 + EPS
            end
            f.erase! if inside
          end
        end
        ents.grep(Sketchup::Edge).each { |e| e.erase! if e.valid? && e.faces.empty? }
        faces = ents.grep(Sketchup::Face)
        raise 'the openings left nothing of the profile' if faces.empty?

        faces.each { |f| f.reverse! if f.normal.dot(lf.normal).negative? }
        faces.each { |f| f.pushpull(depth) if f.valid? }
      end

      # Every solid is compared with the volume its plan describes. Group#volume answers only for manifold solids; a
      # wall whose opening touches its top at one point is closed but pinched (an edge bounds four faces), so for such
      # a solid every edge must bound an even, non-zero number of faces and the volume comes from its face meshes.
      def check_volume(group, expected, el, what, stats)
        if group.manifold?
          vol = group.volume.to_f
        else
          open = group.entities.grep(Sketchup::Edge).count { |e| e.faces.empty? || e.faces.length.odd? }
          if open.positive?
            raise BuildError.new("#{what} is not a closed solid: #{open} edge#{open == 1 ? '' : 's'} bound an odd " \
                                 'number of faces', el[:id])
          end
          vol = mesh_volume(group.entities)
          stats['pinched_solids'] << "#{el[:id]}: #{what}"
        end
        raise BuildError.new("#{what} did not come out as a solid with outward faces (volume #{fmt(vol)})", el[:id]) if vol <= 0
        if (vol - expected).abs > [expected.abs * 1e-4, 1e-3].max
          raise BuildError.new("#{what} has volume #{fmt(vol)} cu in where the plan describes #{fmt(expected)}", el[:id])
        end

        stats['volume_checks'] += 1
        vol
      end

      # The signed volume of a closed, outward-turned surface: a sixth of the sum of det(a, b, c) over its triangles.
      def mesh_volume(ents)
        six = 0.0
        ents.grep(Sketchup::Face).each do |f|
          mesh = f.mesh(0)
          mesh.polygons.each do |poly|
            p = poly.map { |i| mesh.point_at(i.abs) }
            ax, ay, az = p[0].x.to_f, p[0].y.to_f, p[0].z.to_f
            (1...(p.length - 1)).each do |k|
              bx, by, bz = p[k].x.to_f, p[k].y.to_f, p[k].z.to_f
              cx, cy, cz = p[k + 1].x.to_f, p[k + 1].y.to_f, p[k + 1].z.to_f
              six += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
            end
          end
        end
        six / 6.0
      end

      # ================================================================ creases

      # Draw the creases where different elements meet (see "Creases") and return one row per pair, {a, b, edges,
      # length}, read back from the model. For every two faces of different elements whose boxes touch and whose
      # planes are not parallel, the crease is the stretch of their common line on both closed faces, less what either
      # face already draws as an edge. Two placements meet only where they can show at once (chains_agree?); their
      # crease nests in the tags of both chains, then the two parts' own. `only`, for a build in place, is the ids of
      # the elements built, instanced or erased: the pairs naming one are erased and drawn again, and every other
      # pair's creases stay. With none (nil) every crease is drawn again. `ranks` orders a pair's two elements.
      def draw_creases(model, top, man, ranks, stats, only = nil)
        t0 = Time.now
        untagged = model.layers[0]
        area_of = man ? man['areas'].flat_map { |a| a['tags'].map { |t| [t, a['id']] } }.to_h : {}
        order = man ? man['areas'].map { |a| a['id'] } : []
        container = top.entities.grep(Sketchup::Group).find { |g| g.name == INTERSECTIONS }
        if container && only
          container.entities.grep(Sketchup::Group).each do |pg|
            pg.erase! if only[pg.get_attribute(DICT, 'a')] || only[pg.get_attribute(DICT, 'b')]
          end
        elsif container
          container.erase!
          container = nil
        end
        placed = []
        index = {}
        by_definition = {}
        each_element_group(top) do |zg, g, chain|
          unless g.transformation.identity? && zg.transformation.identity?
            raise BuildError.new('its group has been moved; build.rb draws every solid in absolute coordinates',
                                 element_id(g))
          end
          id = element_id(g)
          index[id] ||= ranks[[id, g.get_attribute(DICT, KEY_ATTR)]] || 0
          next if only && only.empty?

          el = { id: id, index: index[id] }
          proto = by_definition[g.definition] ||= leaves_of(el, g, untagged).map { |l| [l, l.group == g] }
          where = chain.to_h { |t| [area_of[t] || t, t] }
          proto.each do |l, whole|
            placed << [Leaf.new(el, l.group, [zg.layer] + l.tags, l.faces, l.box), where, whole ? id : l.group.name]
          end
        end
        sink = CreaseSink.new(top, container, {}, {}, 0)
        drawn = {}
        sorted = placed.each_with_index.sort_by { |pl, k| [pl[0].box[0], k] }.map(&:first)
        sorted.each_with_index do |(a, wa, na), i|
          ((i + 1)...sorted.length).each do |j|
            b, wb, nb = sorted[j]
            break if b.box[0] > a.box[3] + EPS
            next if only && !only[a.el[:id]] && !only[b.el[:id]]
            next if a.el[:id] == b.el[:id] || !boxes_touch?(a.box, b.box) || !chains_agree?(wa, wb)

            first = [a.el[:index], a.el[:id]] <=> [b.el[:index], b.el[:id]]
            la, lb, pa, pb = first.negative? ? [a, b, na, nb] : [b, a, nb, na]
            segs = crease_segments(la, lb, stats)
            next if segs.empty?

            both = wa.merge(wb)
            chain = order.filter_map { |area| both[area] }
            key = [la.el[:id], lb.el[:id]]
            dest = crease_dest(model, sink, key, [pa, pb], chain.map { |t| model.layers[t] } + (la.tags + lb.tags).uniq)
            drawn[key] = true
            segs.each do |p, q|
              next if dest.entities.add_line(Geom::Point3d.new(*p), Geom::Point3d.new(*q))

              raise BuildError.new("SketchUp refused a #{fmt(dist(p, q))} in crease against #{key[1]}", key[0])
            end
          end
        end
        container = sink.container
        if container && count_faces(container.entities).positive?
          raise BuildError, "the #{INTERSECTIONS} group came out with faces; creases are edges only"
        end
        rows = crease_rows(container, index)
        stats['intersection_pairs_drawn'] = drawn.length
        stats['intersection_pairs'] = rows.length
        stats['intersection_edges'] = rows.sum { |r| r['edges'] }
        stats['intersection_seconds'] = (Time.now - t0).round(2)
        rows
      end

      # One row per pair group of a crease container, in the order of the pairs' elements: the creases the model
      # holds, whichever build drew them.
      def crease_rows(container, index)
        return [] unless container

        length = lambda do |ents|
          ents.sum do |e|
            case e
            when Sketchup::Edge then e.length.to_f
            when Sketchup::Group then length.call(e.entities)
            else 0.0
            end
          end
        end
        rows = container.entities.grep(Sketchup::Group).map do |pg|
          { 'a' => pg.get_attribute(DICT, 'a'), 'b' => pg.get_attribute(DICT, 'b'),
            'edges' => count_edges(pg.entities), 'length' => length.call(pg.entities).round(3) }
        end
        rows.sort_by { |r| [index[r['a']] || 0, index[r['b']] || 0, r['a'].to_s, r['b'].to_s] }
      end

      # The innermost group the creases of one pair of parts go in, made on first use: the container, the pair's group
      # (named for the two elements and recording them), then one nested group per tag, the innermost recording the
      # two parts (render.rb hides the creases of a door it opens by them).
      def crease_dest(model, sink, key, parts, tags)
        sink.dests[key + parts + tags.map(&:name)] ||= begin
          sink.made += 1
          sink.container ||= part(model, sink.top, INTERSECTIONS, INTERSECTIONS_TAG)
          pg = sink.pair_groups[key]
          unless pg
            pg = sink.pair_groups[key] = part(model, sink.container, "#{key[0]} x #{key[1]}")
            pg.set_attribute(DICT, 'a', key[0])
            pg.set_attribute(DICT, 'b', key[1])
          end
          inner = tags.reduce(pg) { |parent, t| part(model, parent, t.name, t.name) }
          inner.set_attribute(DICT, 'part_a', parts[0])
          inner.set_attribute(DICT, 'part_b', parts[1])
          inner
        end
      end

      # The groups of one element that hold faces, each with the tags from the element group down to it (draw_creases
      # puts the zone's and the chain's in front).
      def leaves_of(el, g, untagged)
        out = []
        stack = [[g, []]]
        until stack.empty?
          grp, above = stack.pop
          unless grp.transformation.identity?
            raise BuildError.new("group #{grp.name.inspect} has been moved; build.rb draws every solid in absolute " \
                                 'coordinates', el[:id])
          end
          next if grp.hidden? || NO_CREASE_TAGS.include?(grp.layer.name)

          tags = grp.layer == untagged ? above : above + [grp.layer]
          faces = grp.entities.grep(Sketchup::Face).map { |f| face_data(f) }
          out << Leaf.new(el, grp, tags, faces, union_box(faces.map(&:box))) unless faces.empty?
          grp.entities.grep(Sketchup::Group).each { |sub| stack << [sub, tags] }
        end
        out
      end

      def face_data(face)
        n = face.normal
        len = n.length.to_f
        nx, ny, nz = n.x.to_f / len, n.y.to_f / len, n.z.to_f / len
        verts = face.vertices.map { |v| v.position.to_a.map(&:to_f) }
        d = -(nx * verts[0][0] + ny * verts[0][1] + nz * verts[0][2])
        edges = face.edges.map do |e|
          [*e.start.position.to_a.map(&:to_f), *e.end.position.to_a.map(&:to_f), !(e.soft? || e.hidden?)]
        end
        FaceData.new(face, box_of(verts), [nx, ny, nz], d, verts, edges)
      end

      def crease_segments(la, lb, stats)
        segs = []
        la.faces.each do |fa|
          next unless boxes_touch?(fa.box, lb.box)

          lb.faces.each do |fb|
            next unless boxes_touch?(fa.box, fb.box)

            stats['intersection_face_pairs'] += 1
            face_crease(fa, fb, la, lb, segs)
          end
        end
        merge_collinear(segs)
      end

      # Append to segs the crease between two faces of different elements.
      def face_crease(fa, fb, la, lb, segs)
        return if beside?(fb, fa) || beside?(fa, fb)

        ax, ay, az = fa.normal
        bx, by, bz = fb.normal
        ux = ay * bz - az * by
        uy = az * bx - ax * bz
        uz = ax * by - ay * bx
        s = Math.sqrt(ux * ux + uy * uy + uz * uz)
        return if s < PARALLEL_SIN

        u = [ux / s, uy / s, uz / s]
        # The point of the common line nearest a vertex r of fa is r + c1 na + c2 nb with na . (p - r) = 0 and
        # nb . p + db = 0.
        r = fa.verts[0]
        k = ax * bx + ay * by + az * bz
        h = -(fb.d + bx * r[0] + by * r[1] + bz * r[2])
        c2 = h / (s * s)
        c1 = -k * c2
        o = [r[0] + c1 * ax + c2 * bx, r[1] + c1 * ay + c2 * by, r[2] + c1 * az + c2 * bz]
        on_a, drawn_a = face_intervals(fa, o, u, la, lb)
        return if on_a.empty?

        on_b, drawn_b = face_intervals(fb, o, u, la, lb)
        common = intersect_intervals(on_a, on_b)
        return if common.empty?

        subtract_intervals(common, (drawn_a + drawn_b).sort_by(&:first)).each do |t0, t1|
          segs << [along(o, u, t0), along(o, u, t1)] if t1 - t0 > EPS
        end
      end

      # True when every vertex of f lies more than EPS to one side of the plane of other, so the faces cannot meet.
      def beside?(f, other)
        nx, ny, nz = other.normal
        side = 0
        f.verts.each do |x, y, z|
          dd = nx * x + ny * y + nz * z + other.d
          return false if dd.abs <= EPS

          s = dd.positive? ? 1 : -1
          return false if side != 0 && s != side

          side = s
        end
        true
      end

      # For the line o + t u lying in the plane of f: the stretches of it on the closed face, and the stretches the
      # face already draws as edges, each a sorted list of [t0, t1].
      def face_intervals(f, o, u, la, lb)
        nx, ny, nz = f.normal
        ux, uy, uz = u
        sx = uy * nz - uz * ny   # in the plane, square to the line
        sy = uz * nx - ux * nz
        sz = ux * ny - uy * nx
        ox, oy, oz = o
        ts = []
        drawn = []
        f.edges.each do |ax, ay, az, bx, by, bz, shown|
          da = (ax - ox) * sx + (ay - oy) * sy + (az - oz) * sz
          db = (bx - ox) * sx + (by - oy) * sy + (bz - oz) * sz
          ta = (ax - ox) * ux + (ay - oy) * uy + (az - oz) * uz
          tb = (bx - ox) * ux + (by - oy) * uy + (bz - oz) * uz
          near_a = da.abs <= EPS
          near_b = db.abs <= EPS
          ts << ta if near_a
          ts << tb if near_b
          if near_a && near_b
            drawn << [[ta, tb].min, [ta, tb].max] if shown
          elsif !near_a && !near_b && da.negative? != db.negative?
            ts << ta + (tb - ta) * (da / (da - db))
          end
        end
        return [[], drawn] if ts.length < 2

        ts.sort!
        cuts = [ts[0]]
        ts.each { |t| cuts << t if t - cuts.last > EPS }
        on = []
        cuts.each_cons(2) do |t0, t1|
          mid = (t0 + t1) / 2.0
          c = f.face.classify_point(Geom::Point3d.new(ox + ux * mid, oy + uy * mid, oz + uz * mid))
          if ON_FACE.include?(c)
            if !on.empty? && t0 - on.last[1] <= EPS
              on.last[1] = t1
            else
              on << [t0, t1]
            end
          elsif c != Sketchup::Face::PointOutside
            raise BuildError.new("the crease against #{lb.el[:id]} failed: Face#classify_point answered #{c} for a " \
                                 "point on the face's own plane", la.el[:id])
          end
        end
        [on, drawn]
      end

      def intersect_intervals(a, b)
        out = []
        i = j = 0
        while i < a.length && j < b.length
          lo = [a[i][0], b[j][0]].max
          hi = [a[i][1], b[j][1]].min
          out << [lo, hi] if hi - lo > EPS
          a[i][1] < b[j][1] ? i += 1 : j += 1
        end
        out
      end

      # ints less spans; spans sorted by start, possibly overlapping.
      def subtract_intervals(ints, spans)
        return ints if spans.empty?

        ints.flat_map do |lo, hi|
          pieces = []
          cur = lo
          spans.each do |s0, s1|
            next if s1 <= cur + EPS
            break if s0 >= hi - EPS

            pieces << [cur, s0] if s0 - cur > EPS
            cur = [cur, s1].max
            break if cur >= hi - EPS
          end
          pieces << [cur, hi] if hi - cur > EPS
          pieces
        end
      end

      # One edge per crease, however many face pairs produced pieces of it: collinear segments that overlap or touch
      # are joined.
      def merge_collinear(segs)
        out = []
        segs.each do |seg|
          cur = seg
          loop do
            k = out.index { |other| collinear_overlap?(other, cur) }
            break unless k

            cur = extent(out.delete_at(k), cur)
          end
          out << cur
        end
        out
      end

      def collinear_overlap?(s, q)
        len = dist(s[0], s[1])
        return false if len <= EPS || q.any? { |p| line_dist(s[0], s[1], p) > EPS }

        u = sub(s[1], s[0]).map { |v| v / len }
        ts = q.map { |p| dot(sub(p, s[0]), u) }
        ts.min <= len + EPS && ts.max >= -EPS
      end

      def extent(s, q)
        len = dist(s[0], s[1])
        u = sub(s[1], s[0]).map { |v| v / len }
        pts = s + q
        ts = pts.map { |p| dot(sub(p, s[0]), u) }
        [pts[ts.index(ts.min)], pts[ts.index(ts.max)]]
      end

      def along(o, u, t)
        [o[0] + u[0] * t, o[1] + u[1] * t, o[2] + u[2] * t]
      end

      def box_of(pts)
        (0..2).map { |i| pts.map { |p| p[i] }.min } + (0..2).map { |i| pts.map { |p| p[i] }.max }
      end

      def union_box(boxes)
        (0..2).map { |i| boxes.map { |b| b[i] }.min } + (3..5).map { |i| boxes.map { |b| b[i] }.max }
      end

      def boxes_touch?(p, q)
        p[0] <= q[3] + EPS && q[0] <= p[3] + EPS && p[1] <= q[4] + EPS && q[1] <= p[4] + EPS &&
          p[2] <= q[5] + EPS && q[2] <= p[5] + EPS
      end

      def count_edges(ents)
        ents.sum do |e|
          case e
          when Sketchup::Edge then 1
          when Sketchup::Group then count_edges(e.entities)
          else 0
          end
        end
      end

      def count_faces(ents)
        ents.sum do |e|
          case e
          when Sketchup::Face then 1
          when Sketchup::Group then count_faces(e.entities)
          else 0
          end
        end
      end

      # ================================================================ vectors

      def sub(a, b)
        a.each_with_index.map { |v, i| v - (b[i] || 0.0) }
      end

      def dot(a, b)
        a.each_with_index.sum { |v, i| v * (b[i] || 0.0) }
      end

      def dist(a, b)
        Math.sqrt(sub(a, b).sum { |v| v * v })
      end

      # Distance from q to the line through p and r.
      def line_dist(p, r, q)
        pr = sub(r, p)
        l2 = dot(pr, pr)
        return dist(p, q) if l2 < 1e-18

        t = dot(sub(q, p), pr) / l2
        dist(p.each_with_index.map { |v, i| v + pr[i] * t }, q)
      end

      def area2(pts)
        s = 0.0
        pts.each_with_index do |p, i|
          q = pts[(i + 1) % pts.length]
          s += p[0] * q[1] - q[0] * p[1]
        end
        s / 2.0
      end

      def newell(pts)
        nx = ny = nz = 0.0
        pts.each_with_index do |p, i|
          q = pts[(i + 1) % pts.length]
          nx += (p[1] - q[1]) * (p[2] + q[2])
          ny += (p[2] - q[2]) * (p[0] + q[0])
          nz += (p[0] - q[0]) * (p[1] + q[1])
        end
        [nx, ny, nz]
      end

      # A number for a message: three decimals at most, trailing zeros dropped.
      def fmt(v)
        return v.inspect unless v.is_a?(Numeric)

        s = format('%.3f', v).sub(/0+\z/, '').sub(/\.\z/, '')
        s == '-0' ? '0' : s
      end

      # ================================================================ report

      def new_stats(plan, top_name)
        by_type = Hash.new(0)
        plan[:elements].each { |el| by_type[el[:type]] += 1 }
        { 'ir' => plan[:ir], 'ir_sha256' => plan[:ir_sha256], 'build_sha256' => SOURCE_SHA256, 'top' => top_name,
          'elements' => plan[:elements].length, 'by_type' => by_type, 'phases' => {},
          'built' => 0, 'instanced' => 0, 'kept' => 0, 'erased' => 0, 'moved_containers' => 0,
          'openings' => 0, 'unit_frames' => 0, 'mullions' => 0, 'rails' => 0, 'glass_panes' => 0, 'leaf_panels' => 0,
          'casings' => 0, 'treads' => 0, 'box_seams' => 0, 'box_rails' => 0, 'roof_holes' => 0, 'floor_faces' => 0,
          'ceiling_faces' => 0, 'sloped_ceilings' => 0, 'space_edges_hidden' => 0, 'wall_faces_painted' => 0,
          'volume_checks' => 0, 'pinched_solids' => [], 'intersection_pairs' => 0, 'intersection_edges' => 0,
          'intersection_face_pairs' => 0,
          'warnings' => (plan[:warnings] || []).map { |w| w.is_a?(Hash) ? "#{w[:element_id]}: #{w[:message]}" : w.to_s },
          'report' => top_name == TOP_NAME ? REPORT : nil }
      end

      # One row per element group: id, type, zone, the chain it stands in, its bounds and its face count.
      def element_report(g, chain = [])
        bb = g.bounds
        { 'id' => element_id(g), 'type' => g.get_attribute(DICT, 'type'), 'zone' => g.get_attribute(DICT, 'zone'),
          'chain' => chain,
          'bounds' => [bb.min.x, bb.min.y, bb.min.z, bb.max.x, bb.max.y, bb.max.z].map { |v| v.to_f.round(4) },
          'faces' => count_faces(g.entities) }
      end

      def write_report(stats, rows, creases, path = REPORT)
        FileUtils.mkdir_p(File.dirname(path))
        payload = { 'at' => Time.now.utc.iso8601, 'stats' => stats, 'elements' => rows, 'intersections' => creases }
        tmp = "#{path}.tmp-#{Process.pid}"
        File.write(tmp, JSON.pretty_generate(payload))
        File.rename(tmp, path)
      end

      # ================================================================ views

      # A few overview pictures of a top group (HOUSE by default), written to out/views/<name>/: two shaded isometric
      # views from opposite corners (iso, iso_back), one in the edges style (iso_edges), and a plan looking down on
      # storey 0 with the roof and ceilings hidden (plan). `areas` are the design areas' tags to show, as a render
      # request's (a view's tags from the manifest); with none, each area shows its today, so the pictures are
      # today's house whatever the owner's tags show. Returns the files written.
      def export_views(top_name: TOP_NAME, name: 'today', areas: [], width: 1600, height: 1000)
        raise BuildError, "a views name is lowercase letters, digits and _, got #{name.inspect}" unless
          name.is_a?(String) && name.match?(/\A[a-z][a-z0-9_]*\z/)

        out_dir = File.join(VIEWS_DIR, name)
        model = Sketchup.active_model
        top = house_group(model, top_name)
        raise BuildError, "there is no #{top_name} group to render; build first" unless top

        bb = Geom::BoundingBox.new
        top.entities.grep(Sketchup::Group).each { |zg| bb.add(zg.bounds) unless zg.hidden? }
        raise BuildError, "the #{top_name} group is empty" if bb.empty?

        zones = begin
          JSON.parse(top.get_attribute(DICT, 'zones') || '{}')
        rescue JSON::ParserError
          {}
        end
        upper = zones.select { |_, z| z['storey'].to_f > 0 }.keys.map { |z| "zone/#{z}" }
        plan_hide = (upper + %w[type/roof_plane part/ceiling]).select { |n| model.layers[n] }
        ctr = bb.center
        aspect = width.to_f / height
        views = [['iso', [-1.0, -1.0, 0.8], 'shaded', []], ['iso_edges', [-1.0, -1.0, 0.8], 'edges', []],
                 ['iso_back', [1.0, 1.0, 0.8], 'shaded', []]]
        out = views.map do |name, dir, style, hide|
          d = Geom::Vector3d.new(*dir)
          d.normalize!
          eye = ctr.offset(d, fit_distance(bb, ctr, d, Geom::Vector3d.new(0, 0, 1), 35.0, aspect))
          HouseTwin::Render.run({ 'out' => File.join(out_dir, "#{name}.png"), 'width' => width, 'height' => height,
                                  'eye' => eye.to_a.map(&:to_f), 'target' => ctr.to_a.map(&:to_f), 'up' => [0, 0, 1],
                                  'fov_v_deg' => 35.0, 'style' => style, 'hide_tags' => hide, 'areas' => areas,
                                  'top' => top_name })['out']
        end
        span = [bb.height.to_f, bb.width.to_f / aspect].max * 1.1
        eye = [ctr.x.to_f, ctr.y.to_f, bb.max.z.to_f + 1000.0]
        out << HouseTwin::Render.run({ 'out' => File.join(out_dir, 'plan.png'), 'width' => width, 'height' => height,
                                       'eye' => eye, 'target' => [ctr.x.to_f, ctr.y.to_f, 0.0], 'up' => [0, 1, 0],
                                       'projection' => 'parallel', 'view_height' => span, 'style' => 'edges',
                                       'hide_tags' => plan_hide, 'areas' => areas, 'top' => top_name })['out']
        out
      end

      # The shortest stand-off along dir from ctr at which all eight corners of bb project inside `margin` of the frame.
      def fit_distance(bb, ctr, dir, up, fov_v_deg, aspect, margin = 0.9)
        fwd = dir.reverse
        right = fwd.cross(up)
        right.normalize!
        cam_up = right.cross(fwd)
        tv = Math.tan(fov_v_deg * Math::PI / 360.0) * margin
        th = tv * aspect
        corners = (0..7).map { |i| bb.corner(i) }
        fits = lambda do |dist|
          eye = ctr.offset(dir, dist)
          corners.all? do |p|
            v = p - eye
            z = v.dot(fwd)
            z.positive? && (v.dot(right) / z).abs <= th && (v.dot(cam_up) / z).abs <= tv
          end
        end
        lo = 0.0
        hi = [bb.diagonal.to_f, 1.0].max
        hi *= 2.0 until fits.call(hi)
        50.times do
          mid = (lo + hi) / 2.0
          fits.call(mid) ? hi = mid : lo = mid
        end
        hi
      end
    end
  end

  # The design areas' menu state lives here, outside Build, so it survives the reload every command makes: SketchUp
  # cannot take a menu item back, so an item is added once per session.
  @menu_state = { root: nil, areas: {}, items: {} } unless instance_variable_defined?(:@menu_state)

  def self.menu_state
    @menu_state
  end
end

load File.join(__dir__, 'render.rb')
