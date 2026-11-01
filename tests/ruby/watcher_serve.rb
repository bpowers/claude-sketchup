# frozen_string_literal: true
#
# The real bridge/watcher.rb served under plain Ruby, for tests/test_ruby.py to drive with the real bridge/client.py.
#
#   ruby tests/ruby/watcher_serve.rb /abs/project
#
# The project is a temporary copy holding bridge/watcher.rb and build/ (so the watcher's paths, which come from where
# its file lies, are the copy's). SketchUp's timer is stood in for by a loop that fires it every 10 ms, until the file
# <project>/stop appears or 60 s pass. Drawing needs SketchUp, so a build fails at its first face, which is what a test
# of the error path wants.

require_relative 'sketchup_stub'

root = ARGV.fetch(0)
Sketchup.active_model = Sketchup::Model.new
Sketchup.active_model.title = 'stand-in'
load File.join(root, 'bridge', 'watcher.rb')
HouseTwin::Watcher.start
deadline = Time.now + 60
until File.exist?(File.join(root, 'stop')) || Time.now > deadline
  UI.fire
  sleep 0.01
end
