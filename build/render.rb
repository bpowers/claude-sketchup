# frozen_string_literal: true
#
# render.rb - renders one request (docs/IR.md "Render requests") to a PNG.
#
# A request is exactly what SketchUp renders: a centred pinhole image of width x height pixels whose full vertical
# field of view across the height is fov_v_deg, seen from eye toward target with up; or, with "projection":
# "parallel", an orthographic view view_height inches tall, for plans and elevations. An off-centre photo camera is
# rendered as a larger centred image and cropped in Python (a request's "crop" is echoed, not applied: SketchUp has
# no setter for a camera's principal point).
#
# The field of view. The camera's aspect ratio is left at 0, the viewport's: View#write_image then spans the
# vertical field of view across the image height whatever the window's shape, as measured in SketchUp 2026 on images
# wider than, as wide as and taller than the viewport. Setting Camera#aspect_ratio makes SketchUp apply the field of
# view across the width instead (Camera#fov_is_height? false), so it is never set, and a camera SketchUp reports that
# way is refused rather than written as an image that does not match its request. The result says what SketchUp took.
#
# Styles
#   edges   hidden line: faces in the background colour, black edges and profiles, glass left out (a hidden-line
#           face is opaque and would blank the room behind it; the frame already draws every pane's outline)
#   shaded  material colours, black edges and profiles
# Both: no shadows, axes, sky, ground, fog or watermarks.
#
# What is drawn. Only one top group: HOUSE, or the one a request names in "top" (a test's). Every other top-level
# entity (FURNISHINGS, the owner's sketches, a scale figure) is hidden while the image is written, every tag and tag
# folder is on but the tags in hide_tags, and section cuts are off, so an image depends on its request and not on what
# the window was showing. Of each design
# area's tags exactly one is on: the one the request names in "areas", or the area's today; so a photo's render,
# which names none, always shows today's house.
#
# Open doors. Every door is built closed. "hide_leaves" names doors whose units to hide (the frame, glass and leaf
# parts, and the creases drawn against them), for a photo that shows them open; the wall's reveals still outline the
# opening. Only a door with a unit can be named; anything else is refused before anything is drawn.
#
# Build identity. Every result carries the ir, ir_sha256 and built_at the top group records. With "expect_ir_sha256"
# (in the request or from the bridge) the render is refused unless the top group records exactly that sha.
#
# The owner's camera, rendering options, shadow setting, tag and tag folder visibility and top-level visibility are
# captured first and always put back, and the whole render runs inside an operation that is aborted, so it leaves no
# undo step.
#
# Loaded by build.rb; not on its own.

require 'json'
require 'fileutils'

module HouseTwin
  module Render
    STYLES      = %w[edges shaded].freeze
    PROJECTIONS = %w[perspective parallel].freeze
    MAX_SIDE    = 16_384
    SHA256_RE   = /\A[0-9a-f]{64}\z/.freeze
    FOV_TOL     = 1e-6           # deg: how near the field of view SketchUp takes must be to the request's

    BASE_OPTIONS = {
      'BackgroundColor' => [255, 255, 255], 'ForegroundColor' => [0, 0, 0],
      'EdgeColorMode' => 1, # every edge in the foreground colour
      'EdgeDisplayMode' => 1, 'DrawSilhouettes' => true, 'SilhouetteWidth' => 2, 'DrawProfilesOnly' => false,
      'DrawDepthQue' => false, 'ExtendLines' => false, 'DrawLineEnds' => false, 'JitterEdges' => false,
      'DrawBackEdges' => false, 'DrawHorizon' => false, 'DrawGround' => false, 'DisplaySketchAxes' => false,
      'DisplayWatermarks' => false, 'DisplayFog' => false, 'DisplayInstanceAxes' => false,
      'DisplaySectionPlanes' => false, 'DisplaySectionCuts' => false, 'DisplayText' => false, 'DisplayDims' => false, 'DisplayColorByLayer' => false,
      'HideConstructionGeometry' => true, 'DrawHidden' => false, 'DrawHiddenGeometry' => false,
      'DrawHiddenObjects' => false, 'AmbientOcclusion' => false, 'ShowViewName' => false,
      'ModelTransparency' => false, 'MaterialTransparency' => true, 'Texture' => true
    }.freeze
    STYLE_OPTIONS = { 'edges' => BASE_OPTIONS.merge('RenderMode' => 1),
                      'shaded' => BASE_OPTIONS.merge('RenderMode' => 2) }.freeze
    STYLE_HIDDEN_TAGS = { 'edges' => %w[part/glass], 'shaded' => [] }.freeze
    UNIT_PART_TAGS = %w[part/frame part/glass part/leaf].freeze     # what hide_leaves hides of a door's unit

    class << self
      # Render a request: a path to a request file, or the request as a Hash. Returns the result.
      def run(src, expect_ir_sha256: nil)
        t0 = Time.now
        req = read(src)
        model = Sketchup.active_model
        view = model.active_view
        raise BuildError, 'a group or component is open for editing in SketchUp; close it before rendering' if model.active_path

        expected = [req[:expect_ir_sha256], expect_sha(expect_ir_sha256)].compact.uniq
        identity = check_identity(model, expected, req[:top])
        top = Build.house_group(model, req[:top]) or raise BuildError, "there is no top-level #{req[:top]} group to render"
        on, off = area_tags(model, req[:areas], req[:top])
        hide = req[:hide_tags].map do |name|
          model.layers[name] ||
            raise(BuildError, "hide_tags names #{name.inspect}, which is not a tag in this model; the tags are " \
                              "#{model.layers.map(&:name).sort.join(' ')}")
        end
        hide += (STYLE_HIDDEN_TAGS[req[:style]] + off).filter_map { |n| model.layers[n] }
        hide.uniq!
        doors = open_doors(model, req[:hide_leaves], req[:top], on)

        saved = capture(model, view, doors[:groups])
        failure = nil
        facts = nil
        model.start_operation('House twin: render', true)
        begin
          apply_options(model, STYLE_OPTIONS[req[:style]])
          model.shadow_info['DisplayShadows'] = false
          model.layers.each do |l|
            want = !hide.include?(l)
            l.visible = want if l.visible? != want
          end
          saved[:folders].each { |f, vis| f.visible = true unless vis }   # a hidden folder hides every tag in it
          model.entities.grep(Sketchup::Drawingelement).each { |e| e.hidden = true unless e == top || e.hidden? }
          top.hidden = false if top.hidden?
          doors[:groups].each { |g| g.hidden = true unless g.hidden? }
          facts = set_camera(view, req)
          write(view, req)
        rescue Exception => e # rubocop:disable Lint/RescueException
          failure = e
        end
        problems = restore(model, view, saved)
        model.abort_operation
        raise failure if failure
        raise BuildError, "the image was written but putting the view back failed: #{problems.join('; ')}" unless problems.empty?

        result(req, hide, t0).merge('hidden_leaves' => doors[:report], 'camera' => facts, 'areas' => on).merge(identity)
      end

      # ---------------------------------------------------------------- request

      # The request as a hash of symbols, every field checked; `out` must already be an absolute .png path (the client
      # resolves a relative one against the project root before sending).
      def read(src)
        if src.is_a?(Hash)
          raw = src
          label = 'render request'
        else
          path = File.expand_path(src.to_s)
          raise BuildError, "render request not found: #{path}" unless File.file?(path)

          begin
            raw = JSON.parse(File.read(path))
          rescue JSON::ParserError => e
            raise BuildError, "render request #{path} is not valid JSON: #{e.message[0, 200]}"
          end
          label = "render request #{path}"
        end
        f = Build::Fields.new(raw, label)
        out = f.str('out')
        f.fail!("'out' must be an absolute path to a .png file, got #{out.inspect}") unless File.absolute_path?(out) &&
                                                                                           out.downcase.end_with?('.png')
        width = f.int('width')
        height = f.int('height')
        unless width.between?(1, MAX_SIDE) && height.between?(1, MAX_SIDE)
          f.fail!("width and height must each lie in 1..#{MAX_SIDE}, got #{width}x#{height}")
        end
        eye = f.nums('eye', 3)
        target = f.nums('target', 3)
        up = f.nums('up', 3)
        projection = f.str('projection', PROJECTIONS, 'perspective')
        fov = view_height = nil
        if projection == 'parallel'
          view_height = f.num('view_height')
          f.fail!("'view_height' must be positive, got #{view_height}") unless view_height.positive?
        else
          fov = f.num('fov_v_deg')
          f.fail!("'fov_v_deg' must lie strictly between 0 and 180, got #{fov}") unless fov.positive? && fov < 180
        end
        expect = f.str('expect_ir_sha256', nil, nil)
        f.fail!("'expect_ir_sha256' must be 64 lowercase hex digits, got #{expect.inspect}") if expect && !SHA256_RE.match?(expect)
        top = f.str('top', nil, Build::TOP_NAME)
        f.fail!("'top' must name a top-level group") if top.strip.empty?
        dir = Build.sub(target, eye)
        dlen = Math.sqrt(Build.dot(dir, dir))
        ulen = Math.sqrt(Build.dot(up, up))
        f.fail!('eye and target coincide') if dlen < 1e-6
        f.fail!("'up' has no length") if ulen < 1e-9
        cross = [dir[1] * up[2] - dir[2] * up[1], dir[2] * up[0] - dir[0] * up[2], dir[0] * up[1] - dir[1] * up[0]]
        f.fail!("'up' is parallel to the view direction") if Math.sqrt(Build.dot(cross, cross)) < 1e-6 * dlen * ulen
        { out: out, width: width, height: height, eye: eye, target: target, up: up, projection: projection, fov: fov,
          view_height: view_height, style: f.str('style', STYLES), transparent: f.bool('transparent', false),
          hide_tags: f.strings('hide_tags'), hide_leaves: f.strings('hide_leaves').uniq,
          areas: f.strings('areas').uniq, expect_ir_sha256: expect, top: top, crop: raw['crop'] }
      end

      def expect_sha(v)
        return nil if v.nil?
        raise BuildError, "expect_ir_sha256 must be 64 lowercase hex digits, got #{v.inspect[0, 80]}" unless v.is_a?(String) && SHA256_RE.match?(v)

        v
      end

      # [the area tags on, the area tags off] while drawing: of each design area the top group was synced with, the
      # tag `named` names, or the area's today. Refuses a name that is no area tag, and two tags of one area.
      def area_tags(model, named, top_name = Build::TOP_NAME)
        areas = top_name == Build::TOP_NAME ? Build.area_list(model) : []
        have = areas.flat_map { |a| a['tags'] }
        unknown = named - have
        unless unknown.empty?
          raise BuildError, "areas names #{unknown.join(', ')}, which #{unknown.length == 1 ? 'is no area tag' : 'are no area tags'} " \
                            "of this model (#{have.empty? ? 'it has none' : have.join(' ')}); sync the areas first (look.py)"
        end
        on = areas.map do |a|
          chosen = named & a['tags']
          raise BuildError, "areas names #{chosen.join(' and ')}, two alternatives of #{a['id']}" if chosen.length > 1

          chosen.first || a['today']
        end
        [on, have - on]
      end

      # What the top group was built from, as result fields; refuses unless every expected sha is the one it records.
      def check_identity(model, expected, top_name)
        ident = Build.identity(model, top_name)
        have = ident && ident['ir_sha256']
        unless expected.all? { |x| x == have }
          held = if ident.nil? then "no #{top_name} group"
                 elsif have.nil? then "a #{top_name} that records no sha256 (rebuild it)"
                 else "#{ident['ir']} (sha256 #{have[0, 12]}, built #{ident['built_at']})"
                 end
          raise BuildError, "refusing to render: the request expects the house with sha256 " \
                            "#{expected.map { |x| x[0, 12] }.join(' and ')}, but SketchUp holds #{held}"
        end
        { 'ir' => ident && ident['ir'], 'ir_sha256' => have, 'built_at' => ident && ident['built_at'] }
      end

      # ---------------------------------------------------------------- state

      # nested: the groups inside the top group whose hidden flag the render sets.
      def capture(model, view, nested = [])
        ro = model.rendering_options
        c = view.camera
        cam = Sketchup::Camera.new(c.eye, c.target, c.up, c.perspective?, c.perspective? ? c.fov : 30.0)
        cam.height = c.height unless c.perspective?
        cam.aspect_ratio = c.aspect_ratio
        cam.image_width = c.image_width
        cam.description = c.description.to_s
        keys = ro.keys
        { camera: cam,
          options: STYLE_OPTIONS.values.flat_map(&:keys).uniq.select { |k| keys.include?(k) }.to_h { |k| [k, ro[k]] },
          shadows: model.shadow_info['DisplayShadows'],
          layers: model.layers.map { |l| [l, l.visible?] },
          folders: tag_folders(model).map { |f| [f, f.visible?] },
          hidden: model.entities.grep(Sketchup::Drawingelement).map { |e| [e, e.hidden?] },
          nested: nested.map { |e| [e, e.hidden?] } }
      end

      # Every tag folder of the model, nested ones too ([] where SketchUp has no folders).
      def tag_folders(model)
        walk = ->(c) { c.respond_to?(:folders) ? c.folders.flat_map { |f| [f] + walk.call(f) } : [] }
        walk.call(model.layers)
      end

      def apply_options(model, opts)
        ro = model.rendering_options
        keys = ro.keys
        opts.each { |k, v| ro[k] = v.is_a?(Array) ? Sketchup::Color.new(*v) : v if keys.include?(k) }
      end

      # Put everything back, collecting problems rather than raising, so one failure never leaves the rest undone.
      def restore(model, view, s)
        problems = []
        step = lambda do |what|
          what.call
        rescue StandardError => e
          problems << "#{e.class}: #{e.message}"
        end
        step.call(-> { s[:options].each { |k, v| model.rendering_options[k] = v } })
        step.call(-> { model.shadow_info['DisplayShadows'] = s[:shadows] })
        step.call(-> { s[:layers].each { |l, vis| l.visible = vis if l.valid? && l.visible? != vis } })
        step.call(-> { s[:folders].each { |f, vis| f.visible = vis if f.valid? && f.visible? != vis } })
        step.call(-> { s[:hidden].each { |e, h| e.hidden = h if e.valid? && e.hidden? != h } })
        step.call(-> { s[:nested].each { |e, h| e.hidden = h if e.valid? && e.hidden? != h } })
        step.call(-> { view.camera = s[:camera] })
        problems
      end

      # ---------------------------------------------------------------- doors

      # What hide_leaves hides, as {groups:, report:}: each named door's unit parts (the groups in its wall recording
      # house_twin/opening), the innermost crease groups naming one of those parts, and one report row per door.
      # Refuses, naming every problem, unless each id is a door with a unit whose parts the model holds. Under HOUSE
      # the walls are those showing while the area tags `on` are on.
      def open_doors(model, ids, top_name, on)
        return { groups: [], report: [] } if ids.empty?

        top = Build.house_group(model, top_name)
        raise BuildError, "hide_leaves names doors, but there is no #{top_name} group" unless top

        walls = Build.shown_elements(model, on, top_name)
        found = {}
        walls.each do |wall_id, g|
          next unless g.get_attribute(Build::DICT, 'type') == 'wall'

          openings = begin
            JSON.parse(g.get_attribute(Build::DICT, 'json').to_s)['openings']
          rescue JSON::ParserError
            nil
          end
          (openings.is_a?(Array) ? openings : []).each do |o|
            next unless o.is_a?(Hash) && ids.include?(o['id'])

            parts = g.entities.grep(Sketchup::Group).select do |pg|
              pg.get_attribute(Build::DICT, 'opening') == o['id'] && UNIT_PART_TAGS.include?(pg.layer.name)
            end
            found[o['id']] = { wall: wall_id, kind: o['kind'], unit: o['unit'].is_a?(Hash), parts: parts }
          end
        end
        problems = ids.filter_map do |id|
          d = found[id]
          if d.nil? then "#{id} is not an opening of a wall showing in the model"
          elsif d[:kind] != 'door' then "#{id} is a #{d[:kind]} opening, and only a door's leaves open"
          elsif !d[:unit] then "#{id} is a door with no unit, drawn as a plain hole already"
          elsif d[:parts].empty? then "#{id} has a unit, but the model holds none of its parts; rebuild"
          end
        end
        raise BuildError, "hide_leaves refused: #{problems.join('; ')}" unless problems.empty?

        creases = crease_groups(top)
        groups = []
        report = ids.map do |id|
          d = found[id]
          names = d[:parts].map(&:name)
          drawn = creases.select { |_, a, b, pa, pb| (a == d[:wall] && names.include?(pa)) || (b == d[:wall] && names.include?(pb)) }
                         .map(&:first)
          groups.concat(d[:parts], drawn)
          { 'opening' => id, 'wall' => d[:wall], 'parts' => names.sort,
            'crease_edges' => drawn.sum { |cg| cg.entities.grep(Sketchup::Edge).length } }
        end
        { groups: groups.uniq, report: report }
      end

      # [group, a, b, part_a, part_b] for every innermost crease group (build.rb crease_dest).
      def crease_groups(top)
        container = top.entities.grep(Sketchup::Group).find { |g| g.name == Build::INTERSECTIONS }
        return [] unless container

        container.entities.grep(Sketchup::Group).flat_map do |pair|
          a = pair.get_attribute(Build::DICT, 'a')
          b = pair.get_attribute(Build::DICT, 'b')
          out = []
          stack = pair.entities.grep(Sketchup::Group)
          until stack.empty?
            g = stack.pop
            part_a = g.get_attribute(Build::DICT, 'part_a')
            out << [g, a, b, part_a, g.get_attribute(Build::DICT, 'part_b')] if part_a
            stack.concat(g.entities.grep(Sketchup::Group))
          end
          out
        end
      end

      # ---------------------------------------------------------------- camera

      # Sets the request's camera and returns what SketchUp took.
      def set_camera(view, req)
        eye = Geom::Point3d.new(*req[:eye])
        target = Geom::Point3d.new(*req[:target])
        up = Geom::Vector3d.new(*req[:up])
        return set_parallel_camera(view, req, eye, target, up) if req[:projection] == 'parallel'

        view.camera = Sketchup::Camera.new(eye, target, up, true, req[:fov])
        got = view.camera
        raise BuildError, 'SketchUp did not take a perspective camera' unless got.perspective?
        unless got.fov_is_height?
          raise BuildError, 'SketchUp reports the field of view across the image width, not its height, so the ' \
                            'image would not match the request'
        end
        if (got.fov.to_f - req[:fov]).abs > FOV_TOL
          raise BuildError, "SketchUp took a field of view of #{got.fov.to_f} where the request asks #{req[:fov]}"
        end

        check_moved(got, eye, target)
        { 'fov' => got.fov.to_f, 'fov_is_height' => true, 'aspect_ratio' => got.aspect_ratio.to_f,
          'viewport' => [view.vpwidth.to_f, view.vpheight.to_f] }
      end

      def set_parallel_camera(view, req, eye, target, up)
        cam = Sketchup::Camera.new(eye, target, up, false)
        cam.height = req[:view_height]
        view.camera = cam
        got = view.camera
        raise BuildError, 'SketchUp did not take a parallel camera' if got.perspective?
        if (got.height.to_f - req[:view_height]).abs > 1e-6
          raise BuildError, "SketchUp set the view height to #{got.height.to_f} for a requested #{req[:view_height]}"
        end

        check_moved(got, eye, target)
        { 'projection' => 'parallel', 'height' => got.height.to_f, 'aspect_ratio' => got.aspect_ratio.to_f,
          'viewport' => [view.vpwidth.to_f, view.vpheight.to_f] }
      end

      # SketchUp may slide a camera's target along its line of sight, which changes nothing in the picture; it must
      # not move the eye or turn the view.
      def check_moved(got, eye, target)
        moved = got.eye.distance(eye).to_f
        turned = eye.vector_to(target).angle_between(got.eye.vector_to(got.target)) * 180.0 / Math::PI
        return if moved <= 1e-3 && turned <= 1e-4

        raise BuildError, "SketchUp moved the camera: its eye by #{moved} in and its view by #{turned} degrees"
      end

      # ---------------------------------------------------------------- output

      def write(view, req)
        out = req[:out]
        FileUtils.mkdir_p(File.dirname(out))
        File.delete(out) if File.exist?(out)
        ok = view.write_image(filename: out, width: req[:width], height: req[:height], antialias: true,
                              transparent: req[:transparent])
        raise BuildError, "write_image returned #{ok.inspect} for #{out}" unless ok && File.file?(out)

        got = png_size(out)
        return if got == [req[:width], req[:height]]

        raise BuildError, "#{out} came out #{got.join('x')} where the request asked for #{req[:width]}x#{req[:height]}"
      end

      def png_size(path)
        File.open(path, 'rb') do |fh|
          head = fh.read(24)
          raise BuildError, "#{path} is not a PNG" unless head && head.bytesize == 24 && head[0, 8] == "\x89PNG\r\n\x1A\n".b

          head[16, 8].unpack('NN')
        end
      end

      def result(req, hide, t0)
        { 'out' => req[:out], 'width' => req[:width], 'height' => req[:height], 'style' => req[:style],
          'projection' => req[:projection], 'transparent' => req[:transparent], 'eye' => req[:eye],
          'target' => req[:target], 'up' => req[:up], 'fov_v_deg' => req[:fov], 'view_height' => req[:view_height],
          'focal_px' => req[:fov] ? (req[:height] / 2.0) / Math.tan(req[:fov] * Math::PI / 360.0) : nil,
          'principal_px' => [req[:width] / 2.0, req[:height] / 2.0], 'top' => req[:top],
          'hidden_tags' => hide.map(&:name), 'crop' => req[:crop], 'seconds' => (Time.now - t0).round(3) }
      end
    end
  end
end
