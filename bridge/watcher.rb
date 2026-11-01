# frozen_string_literal: true
#
# watcher.rb - the file bridge into SketchUp.
#
# SketchUp has no remote API, but it has a Ruby interpreter and a timer. This polls bridge/trigger four times a
# second; when a new request appears there it runs it and answers in out/bridge/<id>.json, so anything that can write
# a file can drive SketchUp. bridge/client.py is the Python end; bridge/README.md has the
# protocol.
#
# Install once, from SketchUp's Ruby Console (Extensions > Developer > Ruby Console):
#
#   load "/abs/path/to/project/bridge/watcher.rb"; HouseTwin::Watcher.install!; HouseTwin::Watcher.start
#
# install! writes a small loader into SketchUp's Plugins folder that records this project's path and loads this very
# file at every start, so an edit to it takes effect on the next start, or at once with a hot reload (`load` it again;
# the timer and the state survive, constants are replaced). Every project path comes from where this file lies.
#
# Requests (one JSON object, written atomically to bridge/trigger):
#   {"bridge": 1, "id": "<unique>", "cmd": "ping"}
#   ... "cmd": "eval",   "code": "Sketchup.version"
#   ... "cmd": "build",  "path": "/abs/plan.json", "top": "HOUSE"
#   ... "cmd": "sync",   "path": "/abs/out/areas/manifest.json", "rebuild": false
#   ... "cmd": "built"
#   ... "cmd": "render", "path": "/abs/request.json" | "request": {...}, "expect_ir_sha256": "<optional>"
#   ... "cmd": "views",  "name": "today" | "<view>", "areas": [tags]
#   ... "cmd": "save",   "path": null | "/abs/copy.skp"
# Each reply goes first as {"state": "running"} and then as "ok" with "result" or "error" with "error": {class,
# message, backtrace, element_id?}, mirrored to out/status.json. build.rb and render.rb are loaded again for every
# command that uses them, so an edit to either takes effect on the next request.

require 'fileutils'
require 'json'
require 'time'

module HouseTwin
  module Watcher
    %i[ROOT TRIGGER BUILD OUT STATUS REPLIES LAST SKP PERIOD PROTOCOL REPLY_TTL PENDING_AGE COARSE ID_RE
       LOADER].each do |c|
      remove_const(c) if const_defined?(c, false)
    end

    ROOT      = File.expand_path('..', __dir__)
    TRIGGER   = File.join(ROOT, 'bridge', 'trigger')
    BUILD     = File.join(ROOT, 'build', 'build.rb')
    OUT       = File.join(ROOT, 'out')
    STATUS    = File.join(OUT, 'status.json')
    REPLIES   = File.join(OUT, 'bridge')
    LAST      = File.join(REPLIES, 'last_id')      # the id of the last request picked up
    SKP       = File.join(ROOT, 'house.skp')       # the project's model file, which save writes with no path
    LOADER    = 'house_twin_loader.rb'             # in SketchUp's Plugins folder (install!)
    # A File.mtime four times a second costs nothing, and it is what stands between a request and SketchUp seeing it.
    # A reload does not change a running timer's period; start does.
    PERIOD    = 0.25
    PROTOCOL  = 1
    REPLY_TTL = 24 * 3600.0                        # s: older reply files are pruned
    PENDING_AGE = 600.0                            # s: a trigger this recent, never picked up, is answered on start
    # s: the coarsest file time stamp a project may sit on (FAT32 keeps 2 s, HFS+ 1 s). Two requests written within it
    # can share an mtime, so for this long after a write the trigger's id is read too.
    COARSE    = 3.0
    ID_RE     = /\A[A-Za-z0-9._-]{1,128}\z/.freeze

    # State survives a reload; only what is not yet defined is set.
    @timer   = nil   unless instance_variable_defined?(:@timer)
    @last    = nil   unless instance_variable_defined?(:@last)
    @busy    = false unless instance_variable_defined?(:@busy)
    @last_id = nil   unless instance_variable_defined?(:@last_id)
    @binding = nil   unless instance_variable_defined?(:@binding)

    class << self
      attr_reader :timer

      def start
        stop
        FileUtils.mkdir_p(REPLIES)
        @last = pending? ? nil : stamp
        @last_id = File.file?(LAST) ? File.read(LAST).strip : nil
        @timer = UI.start_timer(PERIOD, true) { tick }
        begin
          load BUILD                                 # so the design areas' menu is there from the start
          HouseTwin::Build.refresh_menu
        rescue Exception => e # rubocop:disable Lint/RescueException
          note("build.rb did not load: #{e.class}: #{e.message}", 'error')
        end
        note("watching #{TRIGGER} every #{PERIOD} s (protocol #{PROTOCOL})")
        @timer
      end

      def stop
        UI.stop_timer(@timer) if @timer
        @timer = nil
      end

      def running?
        !@timer.nil?
      end

      def stamp
        File.exist?(TRIGGER) ? File.mtime(TRIGGER).to_f : nil
      rescue SystemCallError
        nil
      end

      # Whether the trigger holds a well-formed request other than the last one handled.
      def unseen?
        id = JSON.parse(File.read(TRIGGER))['id']
        id.is_a?(String) && ID_RE.match?(id) && id != @last_id
      rescue StandardError
        false
      end

      # Whether the trigger holds a request a client may still be waiting on: written in the last PENDING_AGE seconds
      # and never picked up (LAST records the id of the last one picked up). start answers it, so a request sent while
      # SketchUp was still starting is not lost, and one already picked up, even by a session that then crashed, is
      # never run twice.
      def pending?
        return false unless File.file?(TRIGGER) && Time.now - File.mtime(TRIGGER) < PENDING_AGE

        id = JSON.parse(File.read(TRIGGER))['id']
        id.is_a?(String) && ID_RE.match?(id) && (File.file?(LAST) ? File.read(LAST).strip : nil) != id
      rescue StandardError
        false
      end

      # A new request is a new mtime, or, within COARSE seconds of the last one, a new id under the same mtime.
      def tick
        return if @busy

        now = stamp
        return if now.nil?
        return if now == @last && !(Time.now.to_f - now < COARSE && unseen?)

        @last = now
        @busy = true
        begin
          handle(File.read(TRIGGER))
        rescue Exception => e # rubocop:disable Lint/RescueException
          # nothing may escape into the timer, or the bridge dies silently
          note("#{e.class}: #{e.message}", 'error', 'backtrace' => (e.backtrace || []).first(40))
        ensure
          @busy = false
        end
      end

      # ---------------------------------------------------------------- requests

      def parse(text)
        h = JSON.parse(text.to_s)
        raise ArgumentError, 'a bridge request is a JSON object with "bridge", "id" and "cmd"' unless h.is_a?(Hash)
        raise ArgumentError, "this watcher speaks protocol #{PROTOCOL}, the request #{h['bridge'].inspect}" unless h['bridge'] == PROTOCOL
        unless h['id'].is_a?(String) && ID_RE.match?(h['id'])
          raise ArgumentError, "a request id must match #{ID_RE.source}, got #{h['id'].inspect}"
        end
        raise ArgumentError, "request #{h['id']} has no cmd" unless h['cmd'].is_a?(String)

        h
      end

      def handle(text)
        req = {}
        t0 = Time.now
        req = parse(text)
        @last_id = req['id']
        File.write(LAST, req['id'])
        reply(req, 'running', "#{req['cmd']} running", 'started_at' => t0.utc.iso8601(3))
        out = dispatch(req)
        reply(req, 'ok', "#{req['cmd']} ok", out.merge('seconds' => (Time.now - t0).round(3),
                                                       'started_at' => t0.utc.iso8601(3)))
      rescue Exception => e # rubocop:disable Lint/RescueException
        err = { 'class' => e.class.name, 'message' => e.message, 'backtrace' => (e.backtrace || []).first(40) }
        err['element_id'] = e.element_id if e.respond_to?(:element_id) && e.element_id
        reply(req, 'error', "#{e.class}: #{e.message}", 'error' => err, 'seconds' => (Time.now - t0).round(3))
      end

      def dispatch(req)
        case req['cmd']
        when 'ping'
          { 'result' => { 'protocol' => PROTOCOL, 'ruby' => RUBY_VERSION, 'sketchup' => Sketchup.version,
                          'model' => Sketchup.active_model&.title, 'model_path' => Sketchup.active_model&.path,
                          'project' => ROOT, 'watcher' => __FILE__, 'running' => running? } }
        when 'eval'
          value = evaluate(req['code'])
          { 'result' => jsonable(value), 'eval' => value.inspect }
        when 'build'
          load BUILD
          { 'result' => HouseTwin::Build.run(need(req, 'path'), top_name: req['top'] || HouseTwin::Build::TOP_NAME) }
        when 'sync'
          load BUILD
          { 'result' => HouseTwin::Build.sync(need(req, 'path'), rebuild: req['rebuild'] == true) }
        when 'built'
          load BUILD
          { 'result' => HouseTwin::Build.identity }
        when 'render'
          load BUILD
          src = req['request'] || req['path']
          raise ArgumentError, 'render needs "path" (a request file) or "request" (an object)' unless src

          { 'result' => HouseTwin::Render.run(src, expect_ir_sha256: req['expect_ir_sha256']) }
        when 'views'
          load BUILD
          areas = req['areas'] || []
          raise ArgumentError, 'views "areas" must be a list of tags' unless areas.is_a?(Array) && areas.all?(String)

          { 'result' => HouseTwin::Build.export_views(name: req['name'] || 'today', areas: areas) }
        when 'save'
          { 'result' => save(req['path']) }
        else
          raise ArgumentError, "unknown bridge command #{req['cmd'].inspect}"
        end
      end

      def need(req, key)
        req[key].is_a?(String) ? req[key] : raise(ArgumentError, "#{req['cmd']} needs \"#{key}\"")
      end

      # Evaluated at top level in one persistent binding, so a local variable one request sets is there for the next.
      def evaluate(code)
        raise ArgumentError, 'eval needs "code"' unless code.is_a?(String)

        @binding ||= TOPLEVEL_BINDING.eval('binding')
        eval(code, @binding, '(bridge eval)', 1) # rubocop:disable Security/Eval
      end

      # With no path, the open model is saved as the project's house.skp: an untitled model becomes that file, and a
      # model opened from it is saved in place. A model opened from anywhere else is refused, never written over
      # house.skp. With a path, a copy is written there and the open model keeps its own path.
      def save(path)
        m = Sketchup.active_model
        if path.nil? || path.to_s.empty?
          current = m.path.to_s
          unless current.empty? || File.expand_path(current) == SKP
            raise "the open model is #{current}, not #{SKP}; save will not write another model over the project's " \
                  'file. Give save a path to write a copy.'
          end
          raise "saving #{SKP} failed" unless current.empty? ? m.save(SKP) : m.save

          { 'saved' => m.path }
        else
          dest = File.expand_path(path)
          raise "save_copy to #{dest} failed" unless m.save_copy(dest)

          { 'saved_copy' => dest }
        end
      end

      # ---------------------------------------------------------------- replies

      def reply(req, state, msg, extra = {})
        payload = { 'state' => state, 'message' => msg, 'at' => Time.now.utc.iso8601(3), 'protocol' => PROTOCOL,
                    'sketchup' => Sketchup.version, 'model' => Sketchup.active_model&.title }
        payload['id'] = req['id'] if req['id']
        payload['cmd'] = req['cmd'] if req['cmd']
        payload = jsonable(payload.merge(extra))
        write_json(STATUS, payload)
        if req['id'].is_a?(String) && ID_RE.match?(req['id'])
          write_json(File.join(REPLIES, "#{req['id']}.json"), payload)
          prune_replies unless state == 'running'
        end
        puts "[house twin] #{state}: #{msg}" unless state == 'running'
        payload
      end

      # status.json only, for what answers no request.
      def note(msg, state = 'ok', extra = {})
        payload = { 'state' => state, 'message' => msg, 'at' => Time.now.utc.iso8601(3), 'protocol' => PROTOCOL,
                    'sketchup' => Sketchup.version, 'model' => Sketchup.active_model&.title }.merge(extra)
        write_json(STATUS, jsonable(payload))
        puts "[house twin] #{state}: #{msg}"
        payload
      rescue StandardError
        nil
      end

      def write_json(path, payload)
        FileUtils.mkdir_p(File.dirname(path))
        tmp = "#{path}.tmp-#{Process.pid}-#{rand(1 << 30)}"
        File.write(tmp, JSON.pretty_generate(payload))
        File.rename(tmp, path)
      end

      def prune_replies
        cutoff = Time.now - REPLY_TTL
        Dir.glob(File.join(REPLIES, '*.json')).each do |f|
          File.delete(f) if File.mtime(f) < cutoff
        rescue SystemCallError
          nil
        end
      end

      # Anything a command returns, reduced to what JSON carries. SketchUp's Length prints architecturally and is not
      # a Float, so lengths and floats go through to_f; anything else unknown becomes its inspect string.
      def jsonable(v, depth = 0)
        return v.inspect if depth > 32

        case v
        when nil, true, false, Integer, String then v
        when Float then v.finite? ? v : v.to_s
        when Symbol then v.to_s
        when Array then v.map { |x| jsonable(x, depth + 1) }
        when Hash then v.each_with_object({}) { |(k, x), h| h[k.to_s] = jsonable(x, depth + 1) }
        else
          if defined?(Length) && v.is_a?(Length) then v.to_f
          elsif v.is_a?(Geom::Point3d) || v.is_a?(Geom::Vector3d) then v.to_a.map(&:to_f)
          elsif v.is_a?(Sketchup::Color) then v.to_a
          else v.inspect
          end
        end
      end

      # ---------------------------------------------------------------- install

      # Writes the loader into SketchUp's Plugins folder: it records this project's path and loads this file from
      # there at every start, then starts the watcher once SketchUp has settled. Returns the loader's path.
      def install!
        plugins = Sketchup.find_support_file('Plugins') or raise 'SketchUp has no Plugins folder to install into'
        loader = File.join(plugins, LOADER)
        File.write(loader, <<~RUBY)
          # Loads the house twin bridge of the project at the path below each time SketchUp starts (bridge/watcher.rb
          # install! wrote this file; run install! again from another checkout to point it there).
          HOUSE_TWIN_PROJECT = #{ROOT.inspect} unless defined?(HOUSE_TWIN_PROJECT)
          watcher = File.join(HOUSE_TWIN_PROJECT, 'bridge', 'watcher.rb')
          if File.file?(watcher)
            load watcher
            UI.start_timer(3.0, false) { HouseTwin::Watcher.start }
          else
            puts "[house twin] no watcher at \#{watcher}; run HouseTwin::Watcher.install! from the project's watcher.rb"
          end
        RUBY
        note("installed #{loader} for #{ROOT}")
        loader
      end
    end
  end
end
