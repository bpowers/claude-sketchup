# frozen_string_literal: true
#
# build.rb's sync, render.rb's request reading and look.py's owner-state probe under plain Ruby, against
# tests/ruby/sketchup_stub.rb, for manifests areasc.py wrote of a real house.
#
#   ruby tests/ruby/sync_cases.rb config.json
#
# tests/test_integration.py writes config.json: {"build_rb", "syncs": [manifest paths, synced in order into one model],
# "state": look.py's STATE code, "requests": [{"request", "expect_ir_sha256"}, ...] as bridge/client.py sends them}.
# Drawing is stubbed (each element's builder adds one empty part) and the crease pass records which elements it was
# told to redraw. Prints one JSON object: for each sync its stats, those elements, every element group HOUSE then
# holds, and the owner's state before and after; for each request what render.rb read of it.

require 'json'
require_relative 'sketchup_stub'

config = JSON.parse(File.read(ARGV.fetch(0)))
load config.fetch('build_rb')

# rubocop:disable Style/GlobalVars
$redrawn = []
module HouseTwin
  module Build
    class << self
      %w[space slab wall column beam stair box roof_plane].each do |t|
        define_method("build_#{t}") { |model, g, _el, _mats, _stats| part(model, g, 'body') }
      end

      def draw_creases(_model, _top, _man, _ranks, _stats, only = nil)
        $redrawn << (only ? only.keys.sort : nil)
        []
      end

      def element_report(g, chain = [])
        { 'id' => element_id(g), 'chain' => chain, 'faces' => 0 }
      end

      def write_report(*); end
    end
  end
end

B = HouseTwin::Build
R = HouseTwin::Render
STATS = %w[in_place built instanced kept erased placements].freeze

model = Sketchup.active_model = Sketchup::Model.new
model.active_layer = B.tag(model, 'mine')     # the owner's current tag, which a build draws nothing on
state = -> { eval(config.fetch('state'), binding, '(look.py STATE)', 1) } # rubocop:disable Security/Eval

syncs = config.fetch('syncs').map do |path|
  before = state.call
  stats = B.sync(path)
  held = []
  B.each_element_group(B.house_group(model)) do |zg, g, chain|
    held << [zg.name.delete_prefix('zone/'), chain, B.element_id(g), g.get_attribute(B::DICT, B::KEY_ATTR)]
  end
  { 'stats' => stats.slice(*STATS), 'redrawn' => $redrawn.last, 'held' => held, 'before' => before,
    'after' => state.call, 'identity' => B.identity(model) }
end

requests = config.fetch('requests').map do |r|
  req = R.read(r.fetch('request'))
  ident = begin
    R.check_identity(model, [R.expect_sha(r['expect_ir_sha256'])].compact, req[:top])
  rescue HouseTwin::BuildError => e
    e.message
  end
  { 'out' => req[:out], 'style' => req[:style], 'projection' => req[:projection], 'fov' => req[:fov],
    'hide_tags' => req[:hide_tags].to_h { |n| [n, !model.layers[n].nil?] },
    'areas_on' => R.area_tags(model, req[:areas], req[:top])[0], 'identity' => ident }
end
# rubocop:enable Style/GlobalVars

puts JSON.generate('syncs' => syncs, 'requests' => requests)
