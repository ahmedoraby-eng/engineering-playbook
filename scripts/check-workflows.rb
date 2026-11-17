# check-workflows.rb [DIR]: strict YAML parse of every workflow under DIR/.github/workflows (default: the current dir) and of
# every composite action file under DIR/.github/actions. A workflow must parse AND have an `on` block (Psych reads the key `on`
# as `true`). Fails closed: no workflow files at all is an error. BASITA-233, 17/11/2026 (an `: ` in a plain scalar made e2e.yml
# invalid and GitHub silently dropped its triggers). ONE copy of this logic: the same file lives at scripts/check-workflows.rb in
# the platform and the playbook (tests/check-workflow-patch.test.sh fails if a copy drifts) and check-workflow-patch.sh runs it.
require "yaml"
root = ARGV[0] || "."
bad = 0
wf = Dir[File.join(root, ".github/workflows/*.{yml,yaml}")].sort
act = Dir[File.join(root, ".github/actions/**/*.{yml,yaml}")].sort
if wf.empty? then puts "FAIL no workflow files under #{root}/.github/workflows"; exit 1 end
(wf + act).each do |f|
  rel = f.sub(%r{\A#{Regexp.escape(root)}/?}, "")
  begin
    d = YAML.safe_load(File.read(f), aliases: true)
    if wf.include?(f) && !(d.is_a?(Hash) && (d["on"] || d[true]))
      puts "FAIL #{rel}: no `on` block"; bad += 1
    else
      puts "ok   #{rel}"
    end
  rescue Psych::SyntaxError, Psych::DisallowedClass, Psych::BadAlias => e
    line = e.respond_to?(:line) ? " at line #{e.line} column #{e.column}" : ""
    puts "FAIL #{rel}: #{e.respond_to?(:problem) ? e.problem : e.message}#{line}"; bad += 1
  end
end
exit(bad.zero? ? 0 : 1)
